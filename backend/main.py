from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import pandas as pd, io, os, json, zipfile, tempfile, re, sqlite3, shutil, glob
from pypdf import PdfReader
from .engine import ProcurementEngine, Policy, RULE_ROUTES
from .policy_parse import parse_policy_text, next_version
from . import chat as chatbot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get('DATA_DIR', os.path.join(ROOT, 'data'))      # everything mutable lives here (mount as a volume)
POLICY_DIR = os.path.join(DATA, 'policies')
DB_PATH = os.path.join(DATA, 'app.db')
SUBMISSION = os.environ.get('SUBMISSION_DIR', os.path.join(ROOT, 'submission'))
SEED_DIRS = [os.path.join(ROOT, 'seed'), '/app/seed']
for d in (DATA, POLICY_DIR, SUBMISSION): os.makedirs(d, exist_ok=True)

app = FastAPI(title='Cantill Procurement Assistant')
app.mount('/static', StaticFiles(directory=os.path.join(ROOT, 'frontend')), name='static')
ENGINE = None
POLICY = Policy()
PDF_CONTRACTS = {}
POLICY_PREVIEW = {}
CSV_COLS = ['pr_no', 'pr_line', 'resolved_item_code', 'group_id', 'po_id', 'decision', 'route', 'supplier_id', 'unit_price',
            'group_value_inr', 'suggested_supplier_ids', 'policy_version', 'reason']
# The task requires the exported decision CSV to use exactly these 13 columns.
# Internal rows may still retain policy_clause/contract_id for the Buyer Workspace and audit trail.
EXPORT_COLS = CSV_COLS


# ---------------- database ----------------
def db():
    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row; return con


def init_db():
    con = db()
    con.executescript('''
    CREATE TABLE IF NOT EXISTS chat_prs (id INTEGER PRIMARY KEY AUTOINCREMENT, pr_no TEXT UNIQUE NOT NULL, pr_line INTEGER NOT NULL,
        session_id TEXT NOT NULL, created_at TEXT NOT NULL, payload_json TEXT NOT NULL, decision_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS chat_sessions (session_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS actions (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, actor TEXT NOT NULL,
        created_at TEXT NOT NULL, details_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS policy_versions (version TEXT PRIMARY KEY, effective_date TEXT NOT NULL, policy_json TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 0, source_filename TEXT, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, filename TEXT, created_at TEXT NOT NULL,
        policy_version TEXT NOT NULL, rows_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS req_status (source TEXT NOT NULL, group_id TEXT NOT NULL, status TEXT NOT NULL, doc_no TEXT, actor TEXT,
        comment TEXT, override_json TEXT, original_json TEXT, updated_at TEXT NOT NULL, PRIMARY KEY (source, group_id));
    CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, source TEXT NOT NULL,
        group_ids TEXT NOT NULL, supplier_id TEXT, actor TEXT, created_at TEXT NOT NULL, details_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS extra_contracts (contract_id TEXT PRIMARY KEY, contract_json TEXT NOT NULL, source TEXT, created_at TEXT NOT NULL);
    ''')
    con.commit(); con.close()


def now(): return str(pd.Timestamp.now())


def record_action(action, details, actor='Procurement'):
    con = db(); con.execute('INSERT INTO actions(action,actor,created_at,details_json) VALUES(?,?,?,?)',
                            (action, actor or 'Unknown', now(), json.dumps(details, default=str))); con.commit(); con.close()


# ---------------- policy persistence ----------------
def persist_policy(p, active=False, source_filename=''):
    with open(os.path.join(POLICY_DIR, f'{p.version}.json'), 'w', encoding='utf-8') as f: json.dump(p.to_dict(), f, indent=2)
    con = db()
    if active: con.execute('UPDATE policy_versions SET active=0')
    con.execute('INSERT OR REPLACE INTO policy_versions(version,effective_date,policy_json,active,source_filename,created_at) VALUES(?,?,?,?,?,?)',
                (p.version, str(p.effective_date), json.dumps(p.to_dict()), 1 if active else 0, source_filename, now()))
    con.commit(); con.close()


def load_persisted_policy():
    con = db(); row = con.execute('SELECT policy_json FROM policy_versions WHERE active=1 ORDER BY created_at DESC LIMIT 1').fetchone(); con.close()
    if row: return Policy.from_dict(json.loads(row['policy_json']))
    p = Policy(); persist_policy(p, active=True, source_filename='Corvo_Procurement_Policy.pdf (baseline v5.0)'); return p


# ---------------- data loading ----------------
def load_csv(name):
    p = os.path.join(DATA, name)
    return pd.read_csv(p) if os.path.exists(p) else pd.DataFrame()


def extract_pdf_contract(source, name=None):
    reader = PdfReader(source); text = '\n'.join((p.extract_text() or '') for p in reader.pages)
    def grab(pattern, default=None):
        m = re.search(pattern, text, re.I | re.S); return m.group(1).strip() if m else default
    cid = grab(r'Contract No\.?\s*([A-Z0-9-]+)') or (os.path.splitext(os.path.basename(name or ''))[0] or None)
    sid = grab(r'Supplier\s+ID\s*([A-Z0-9-]+)')
    start = grab(r'valid from\s*([0-9]{4}-[0-9]{2}-[0-9]{2})')
    end = grab(r'valid from\s*[0-9-]+\s+to\s*([0-9]{4}-[0-9]{2}-[0-9]{2})')
    cap = grab(r'not exceed INR\s*([0-9,]+(?:\.\d+)?)')
    lines = []
    for m in re.finditer(r'(?m)^\s*(?:\d+\s+)?(ITM-\d+)\s+.*?\s([0-9][0-9,]*\.\d{2})\s*$', text):
        lines.append({'item_code': m.group(1), 'rate': float(m.group(2).replace(',', ''))})
    if not (cid and sid and start and end and cap): raise ValueError('Could not extract contract no./supplier/dates/ceiling from PDF')
    if not lines: raise ValueError('No item rate lines found in PDF schedule')
    return {'contract_id': cid, 'supplier_id': sid, 'start_date': start, 'end_date': end, 'value_cap': float(cap.replace(',', '')),
            'consumed_value': 0, 'lines': lines}


def load_pdf_contracts():
    """PDF folder + buyer-added contracts. PDFs already in the register are ignored by the engine (register has consumed values)."""
    global PDF_CONTRACTS
    init_db()
    parsed, errors = {}, []
    d = os.path.join(DATA, 'contracts_pdf')
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.lower().endswith('.pdf'):
                try:
                    c = extract_pdf_contract(os.path.join(d, fn), fn); parsed[c['contract_id']] = c
                except Exception as e: errors.append({'file': fn, 'error': str(e)})
    con = db()
    for r in con.execute('SELECT contract_id, contract_json FROM extra_contracts'): parsed[r['contract_id']] = json.loads(r['contract_json'])
    con.close()
    PDF_CONTRACTS = parsed
    return {'parsed': len(parsed), 'failed': errors}


def load_engine():
    global ENGINE
    ENGINE = ProcurementEngine({'items': load_csv('item_master.csv'), 'suppliers': load_csv('supplier_master.csv'),
        'po': load_csv('po_history.csv'), 'plants': load_csv('plants.csv'), 'contracts': load_csv('contract_register.csv'),
        'contract_lines': load_csv('contract_lines.csv')}, POLICY, PDF_CONTRACTS)
    return ENGINE


def data_loaded(): return ENGINE is not None and not ENGINE.data['items'].empty


def import_kit_root(root):
    pdf_dir = os.path.join(DATA, 'contracts_pdf'); os.makedirs(pdf_dir, exist_ok=True)
    for f in glob.glob(os.path.join(root, 'contracts', 'pdf', '*.pdf')): shutil.copy(f, os.path.join(pdf_dir, os.path.basename(f)))
    for sub, fn in [('master_data', 'item_master.csv'), ('master_data', 'supplier_master.csv'), ('master_data', 'po_history.csv'),
                    ('master_data', 'plants.csv'), ('contracts', 'contract_register.csv'), ('contracts', 'contract_lines.csv'),
                    ('pr_batches', 'sample_pr_batch.csv')]:
        src = os.path.join(root, sub, fn)
        if os.path.exists(src): shutil.copy(src, os.path.join(DATA, fn))
    r = load_pdf_contracts(); load_engine(); return r


def import_kit_zip(raw: bytes):
    with tempfile.TemporaryDirectory() as td:
        zipfile.ZipFile(io.BytesIO(raw)).extractall(td)
        root = None
        for base, dirs, _ in os.walk(td):
            if {'master_data', 'pr_batches', 'contracts'}.issubset(set(dirs)): root = base; break
        if root is None: raise HTTPException(400, 'Invalid task kit: expected master_data/, pr_batches/ and contracts/ folders')
        return import_kit_root(root)


def auto_seed():
    """If no data yet, load a kit ZIP from $TASK_KIT_ZIP or seed/*.zip (lets hosted deployments self-heal after restarts)."""
    if data_loaded(): return
    cands = [os.environ.get('TASK_KIT_ZIP', '')] + [f for d in SEED_DIRS for f in sorted(glob.glob(os.path.join(d, '*.zip')))]
    for f in cands:
        if f and os.path.isfile(f):
            try:
                import_kit_zip(open(f, 'rb').read()); record_action('auto_seed', {'zip': os.path.basename(f)}, 'system'); return
            except Exception as e: print('auto_seed failed', f, e)


def startup_load():
    global POLICY
    init_db(); POLICY = load_persisted_policy(); load_pdf_contracts(); load_engine(); auto_seed()


@app.on_event('startup')
def startup(): startup_load()


@app.get('/')
def home(): return FileResponse(os.path.join(ROOT, 'frontend', 'index.html'))


@app.get('/api/health')
def health():
    return {'ok': True, 'policy_version': POLICY.version, 'effective_date': str(POLICY.effective_date), 'data_loaded': data_loaded(),
            'pdf_contracts': len(PDF_CONTRACTS)}


@app.post('/api/import-kit')
async def import_kit(file: UploadFile = File(...)):
    raw = await file.read()
    if not raw.startswith(b'PK'): raise HTTPException(400, 'Upload the task-kit ZIP')
    r = import_kit_zip(raw); record_action('import_kit', {'pdf_contracts': len(PDF_CONTRACTS), 'pdf_errors': r['failed']})
    return {'ok': True, 'message': f"Task kit imported. {len(PDF_CONTRACTS)} PDF contracts read, {len(r['failed'])} failed.",
            'pdf_contracts': len(PDF_CONTRACTS), 'pdf_errors': r['failed']}


# ---------------- batch processing / runs ----------------
def need_data():
    if not data_loaded(): raise HTTPException(400, 'Import the task kit first (Policy tab)')


def clean_rows(out: pd.DataFrame):
    return json.loads(out.to_json(orient='records'))


def save_run(out, name, filename):
    con = db(); cur = con.execute('INSERT INTO runs(name,filename,created_at,policy_version,rows_json) VALUES(?,?,?,?,?)',
                                  (name, filename, now(), POLICY.version, json.dumps(clean_rows(out)))); rid = cur.lastrowid
    con.commit(); con.close(); return rid


def to_csv_bytes(rows):
    df = pd.DataFrame(rows)
    for c in EXPORT_COLS:
        if c not in df.columns: df[c] = ''
    return df[EXPORT_COLS].to_csv(index=False).encode()


def csv_response(data: bytes, filename):
    return StreamingResponse(io.BytesIO(data), media_type='text/csv', headers={'Content-Disposition': f'attachment; filename={filename}'})


def summarize(rows):
    df = pd.DataFrame(rows)
    return {'lines': len(df), 'requirements': int(df[df.group_id != ''].group_id.nunique()) if len(df) else 0,
            'pos': int(df[df.po_id != ''].po_id.nunique()) if len(df) else 0, 'routes': df.route.value_counts().to_dict() if len(df) else {}}


@app.post('/api/process-batch')
async def process_batch(request: Request, file: UploadFile = File(...), run_name: str = '', format: str = ''):
    """Process a PR batch CSV under the ACTIVE policy. JSON by default; CSV with ?format=csv or Accept: text/csv."""
    need_data()
    raw = await file.read()
    try: df = pd.read_csv(io.BytesIO(raw))
    except Exception as e: raise HTTPException(400, f'Could not read CSV: {e}')
    missing = [c for c in ['pr_no', 'created_at', 'plant', 'quantity', 'need_by_date'] if c not in df.columns]
    if missing: raise HTTPException(400, f'CSV is missing required columns: {missing}')
    out = ENGINE.process(df)
    rid = save_run(out, run_name or f'batch {file.filename}', file.filename)
    if run_name in ('run1', 'run2'): open(os.path.join(SUBMISSION, f'{run_name}.csv'), 'wb').write(to_csv_bytes(clean_rows(out)))
    record_action('process_batch', {'run_id': rid, 'file': file.filename, 'policy_version': POLICY.version, **summarize(clean_rows(out))})
    if format == 'csv' or 'text/csv' in request.headers.get('accept', ''):
        return csv_response(to_csv_bytes(clean_rows(out)), f'decisions_run{rid}.csv')
    return {'run_id': rid, 'policy_version': POLICY.version, 'count': len(out), 'summary': summarize(clean_rows(out)), 'rows': clean_rows(out)}


@app.post('/api/process-sample')
def process_sample():
    need_data(); p = os.path.join(DATA, 'sample_pr_batch.csv')
    if not os.path.exists(p): raise HTTPException(400, 'Sample batch not found; import the task kit')
    out = ENGINE.process(pd.read_csv(p)); rid = save_run(out, 'sample batch', 'sample_pr_batch.csv')
    return {'run_id': rid, 'policy_version': POLICY.version, 'count': len(out), 'rows': clean_rows(out)}


@app.get('/api/runs')
def list_runs():
    con = db(); rows = con.execute('SELECT id,name,filename,created_at,policy_version FROM runs ORDER BY id DESC LIMIT 50').fetchall(); con.close()
    return {'rows': [dict(r) for r in rows]}


def enrich(rows, source):
    items = ENGINE.data['items'].set_index('item_code').description.to_dict() if data_loaded() else {}
    sup = ENGINE.data['suppliers'].set_index('supplier_id') if data_loaded() else None
    con = db(); st = {r['group_id']: dict(r) for r in con.execute('SELECT * FROM req_status WHERE source=?', (source,))}; con.close()
    groups = {}
    for r in rows:
        g = groups.setdefault(r.get('group_id') or f"{r['pr_no']}/{r['pr_line']}", {'group_id': r.get('group_id') or f"{r['pr_no']}/{r['pr_line']}", 'lines': []})
        g['lines'].append({'pr_no': r['pr_no'], 'pr_line': r['pr_line']})
        for k in ('resolved_item_code', 'po_id', 'decision', 'route', 'supplier_id', 'unit_price', 'group_value_inr', 'suggested_supplier_ids',
                  'policy_version', 'policy_clause', 'contract_id', 'reason'): g[k] = r.get(k, '')
    out = []
    for g in groups.values():
        g['item_description'] = items.get(g['resolved_item_code'], '' if g['route'] == 'NOT_PROCESSED' else 'New item (not in item master)')
        g['supplier_name'] = sup.loc[g['supplier_id'], 'name'] if sup is not None and g['supplier_id'] in sup.index else ''
        g['suggested'] = [{'id': s, 'name': sup.loc[s, 'name'] if sup is not None and s in sup.index else ''} for s in str(g['suggested_supplier_ids']).split(';') if s]
        s = st.get(g['group_id'])
        g['status'] = s['status'] if s else ('NOT_PROCESSED' if g['route'] == 'NOT_PROCESSED' else 'PENDING')
        g['doc_no'] = s['doc_no'] if s else ''; g['status_by'] = s['actor'] if s else ''; g['status_comment'] = s['comment'] if s else ''
        g['override'] = json.loads(s['override_json']) if s and s['override_json'] else None
        out.append(g)
    return out


def get_source_rows(source):
    con = db()
    try:
        if source == 'chat':
            rows = [json.loads(r['decision_json']) for r in con.execute('SELECT decision_json FROM chat_prs ORDER BY id')]
            for r in rows: r.setdefault('group_id', r['pr_no'])
            for r in rows: r['group_id'] = r['pr_no']      # one requirement card per chat PR (stored as decided at creation)
            return rows
        r = con.execute('SELECT rows_json FROM runs WHERE id=?', (source,)).fetchone()
        if not r: raise HTTPException(404, 'Run not found')
        return json.loads(r['rows_json'])
    finally: con.close()


@app.get('/api/runs/{source}')
def get_run(source: str):
    rows = get_source_rows(source)
    return {'source': source, 'summary': summarize(rows), 'requirements': enrich(rows, source), 'rows': rows}


@app.get('/api/runs/{source}/export')
def export_run(source: str, save_as: str = ''):
    rows = get_source_rows(source); data = to_csv_bytes(rows)
    if save_as in ('run1', 'run2'): open(os.path.join(SUBMISSION, f'{save_as}.csv'), 'wb').write(data)
    return csv_response(data, f'decisions_{source}.csv')


# ---------------- buyer actions: approve / override (audit trail) ----------------
def _find_group(source, group_id):
    rows = get_source_rows(source); g = [r for r in rows if (r.get('group_id') or '') == group_id]
    if not g: raise HTTPException(404, 'Requirement not found')
    return rows, g


@app.post('/api/requirements/approve')
def approve(payload: dict):
    source, gid, actor = str(payload.get('source')), str(payload.get('group_id')), (payload.get('actor') or '').strip() or 'Buyer'
    rows, g = _find_group(source, gid); d = g[0]
    if d['route'] == 'NOT_PROCESSED': raise HTTPException(400, 'Not processed lines cannot be approved')
    con = db(); ex = con.execute('SELECT status FROM req_status WHERE source=? AND group_id=?', (source, gid)).fetchone()
    if ex and ex['status'] == 'APPROVED': con.close(); raise HTTPException(400, 'Already approved')
    if d['decision'] == 'PO' and d.get('po_id'):
        same = sorted({r['group_id'] for r in rows if r.get('po_id') == d['po_id'] and r['route'] == d['route']
                       and not con.execute("SELECT 1 FROM req_status WHERE source=? AND group_id=? AND status IN ('APPROVED','OVERRIDDEN')", (source, r['group_id'])).fetchone()})
        kind = 'PO'
    else: same, kind = [gid], ('RFQ' if d['route'] == 'RFQ' else 'RFP' if d['route'] == 'RFP' else 'CAPEX_SOURCING' if d['route'] == 'CAPEX_SOURCING' else 'NEW_ITEM_SOURCING')
    cur = con.execute('INSERT INTO documents(kind,source,group_ids,supplier_id,actor,created_at,details_json) VALUES(?,?,?,?,?,?,?)',
                      (kind, source, json.dumps(same), d.get('supplier_id') or '', actor, now(), json.dumps({'po_id': d.get('po_id'), 'route': d['route'], 'policy_version': d['policy_version']})))
    doc_no = f"{kind.split('_')[0]}-{cur.lastrowid:05d}"
    for x in same:
        con.execute('INSERT OR REPLACE INTO req_status(source,group_id,status,doc_no,actor,comment,override_json,original_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                    (source, x, 'APPROVED', doc_no, actor, '', None, None, now()))
    con.commit(); con.close()
    record_action('approve', {'source': source, 'group_ids': same, 'doc_no': doc_no, 'route': d['route']}, actor)
    return {'ok': True, 'doc_no': doc_no, 'kind': kind, 'group_ids': same}


@app.post('/api/requirements/override')
def override(payload: dict):
    source, gid, actor = str(payload.get('source')), str(payload.get('group_id')), (payload.get('actor') or '').strip() or 'Buyer'
    comment, route, sid = (payload.get('comment') or '').strip(), payload.get('route'), (payload.get('supplier_id') or '').strip()
    if len(comment) < 5: raise HTTPException(400, 'An override needs a written reason (at least 5 characters).')
    if route not in RULE_ROUTES | {'NEW_ITEM_SOURCING'}: raise HTTPException(400, f'Route must be one of {sorted(RULE_ROUTES | {"NEW_ITEM_SOURCING"})}')
    rows, g = _find_group(source, gid); d = g[0]
    if route in ('CONTRACT_PO', 'EMERGENCY_PO', 'SPOT_PO'):
        if not sid: raise HTTPException(400, 'A supplier is required for a PO route')
        s = ENGINE.data['suppliers']; x = s[s.supplier_id.astype(str) == sid]
        if x.empty or str(x.iloc[0].status).strip().lower() != 'active':
            raise HTTPException(400, f'Supplier {sid} is not Active: Blocked/Inactive suppliers can never receive a PO (policy 5.7)')
    original = {k: d.get(k) for k in ('decision', 'route', 'supplier_id', 'unit_price', 'group_value_inr', 'suggested_supplier_ids', 'policy_version', 'reason')}
    new = {'route': route, 'supplier_id': sid, 'decision': 'PO' if route.endswith('_PO') else 'SOURCING_REQUEST'}
    con = db()
    con.execute('INSERT OR REPLACE INTO req_status(source,group_id,status,doc_no,actor,comment,override_json,original_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (source, gid, 'OVERRIDDEN', '', actor, comment, json.dumps(new), json.dumps(original), now())); con.commit(); con.close()
    record_action('override', {'source': source, 'group_id': gid, 'comment': comment, 'original': original, 'new': new}, actor)
    return {'ok': True}


@app.get('/api/actions')
def actions(limit: int = 200):
    con = db(); rows = con.execute('SELECT id,action,actor,created_at,details_json FROM actions ORDER BY id DESC LIMIT ?', (limit,)).fetchall(); con.close()
    return {'rows': [{**dict(r), 'details': json.loads(r['details_json'])} for r in rows]}


# ---------------- contracts ----------------
@app.get('/api/contracts')
def contracts():
    need_data(); out = []
    cr, cl = ENGINE.data['contracts'], ENGINE.data['contract_lines']
    for _, c in cr.iterrows():
        out.append({'contract_id': c.contract_id, 'supplier_id': c.supplier_id, 'start_date': str(c.start_date.date()), 'end_date': str(c.end_date.date()),
                    'value_cap': float(c.value_cap), 'consumed_value': float(c.consumed_value), 'items': int((cl.contract_id == c.contract_id).sum()), 'source': 'register'})
    for c in PDF_CONTRACTS.values():
        if c['contract_id'] in set(cr.contract_id): continue
        out.append({'contract_id': c['contract_id'], 'supplier_id': c['supplier_id'], 'start_date': c['start_date'], 'end_date': c['end_date'],
                    'value_cap': c['value_cap'], 'consumed_value': c.get('consumed_value', 0), 'items': len(c['lines']), 'source': 'pdf-only'})
    return {'rows': out}


def _add_contract(c, source, actor):
    con = db(); con.execute('INSERT OR REPLACE INTO extra_contracts(contract_id,contract_json,source,created_at) VALUES(?,?,?,?)', (c['contract_id'], json.dumps(c), source, now()))
    con.commit(); con.close(); load_pdf_contracts(); load_engine(); record_action('add_contract', {'contract': c['contract_id'], 'source': source, 'lines': len(c['lines'])}, actor)


@app.post('/api/contracts/pdf')
async def add_contract_pdf(file: UploadFile = File(...), actor: str = 'Buyer'):
    need_data(); raw = await file.read()
    try: c = extract_pdf_contract(io.BytesIO(raw), file.filename)
    except Exception as e: raise HTTPException(400, f'Could not read contract PDF: {e}')
    _add_contract(c, 'pdf-upload', actor); return {'ok': True, 'contract': c}


@app.post('/api/contracts')
def add_contract_manual(payload: dict):
    need_data()
    try:
        c = {'contract_id': str(payload['contract_id']).strip(), 'supplier_id': str(payload['supplier_id']).strip(), 'start_date': str(pd.Timestamp(payload['start_date']).date()),
             'end_date': str(pd.Timestamp(payload['end_date']).date()), 'value_cap': float(payload['value_cap']), 'consumed_value': float(payload.get('consumed_value') or 0),
             'lines': [{'item_code': str(l['item_code']).strip(), 'rate': float(l['rate'])} for l in payload['lines']]}
    except Exception as e: raise HTTPException(400, f'Invalid contract: {e}')
    if not c['lines']: raise HTTPException(400, 'At least one item line is required')
    bad = [l['item_code'] for l in c['lines'] if l['item_code'] not in set(ENGINE.data['items'].item_code)]
    if bad: raise HTTPException(400, f'Unknown item codes: {bad}')
    _add_contract(c, 'manual', payload.get('actor') or 'Buyer'); return {'ok': True, 'contract': c}


# ---------------- preferred suppliers ----------------
@app.get('/api/preferred')
def preferred(as_of: str = '2026-11-01', top: int = 3):
    need_data(); out = ENGINE.preferred_export(pd.Timestamp(as_of), top)
    return {'rows': json.loads(out.to_json(orient='records')), 'as_of': as_of, 'top': top}


@app.get('/api/preferred/export')
def preferred_export(as_of: str = '2026-11-01', top: int = 3, save: bool = False):
    need_data(); out = ENGINE.preferred_export(pd.Timestamp(as_of), top)[['item_code', 'rank', 'supplier_id', 'reference_price']]
    data = out.to_csv(index=False).encode()
    if save: open(os.path.join(SUBMISSION, 'preferred.csv'), 'wb').write(data)
    return csv_response(data, f'preferred_suppliers_{as_of}.csv')


# ---------------- policy management ----------------
def parse_ctx():
    if not data_loaded(): return {'plants': POLICY.plants}
    it = ENGINE.data['items']
    return {'plants': sorted(set(POLICY.plants) | set(ENGINE.data['plants'].plant.astype(str))), 'categories': sorted(it.category.astype(str).unique()),
            'item_types': sorted(it.item_type.astype(str).unique())}


def make_preview(text, filename):
    global POLICY_PREVIEW
    proposed = parse_policy_text(text, POLICY, parse_ctx())
    POLICY_PREVIEW = {'filename': filename, 'preview': text[:12000], 'proposed': proposed, 'active': POLICY.to_dict()}
    return POLICY_PREVIEW


@app.post('/api/policy')
async def policy_upload(file: UploadFile = File(...)):
    raw = await file.read()
    if file.filename.lower().endswith('.pdf'):
        text = '\n'.join((p.extract_text() or '') for p in PdfReader(io.BytesIO(raw)).pages)
    else: text = raw.decode('utf-8', 'ignore')
    return make_preview(text, file.filename)


@app.post('/api/policy/describe')
def policy_describe(payload: dict):
    """Describe a policy change in plain text (chat-style); returns the same preview as an uploaded amendment."""
    return make_preview(str(payload.get('text', '')), 'typed description')


@app.get('/api/policy/active')
def policy_active(): return POLICY.to_dict()


@app.post('/api/policy/approve')
def approve_policy(u: dict):
    global POLICY, POLICY_PREVIEW
    try:
        d = POLICY.to_dict(); d.update({k: v for k, v in u.items() if k in d and v is not None}); p = Policy.from_dict(d)
    except Exception as e: raise HTTPException(400, f'Invalid policy values: {e}')
    if not p.version or p.version == POLICY.version: raise HTTPException(400, f'Policy version must change before activation (active is {POLICY.version}; try {next_version(POLICY.version)})')
    con = db(); exists = con.execute('SELECT 1 FROM policy_versions WHERE version=?', (p.version,)).fetchone(); con.close()
    if exists: raise HTTPException(400, f'Version {p.version} already exists; choose a new version number')
    old = POLICY.to_dict(); POLICY = p; persist_policy(p, active=True, source_filename=POLICY_PREVIEW.get('filename', '')); POLICY_PREVIEW = {}
    load_engine()
    record_action('activate_policy', {'version': p.version, 'effective_date': str(p.effective_date), 'from_version': old['version'],
                  'changed': {k: [old[k], v] for k, v in p.to_dict().items() if old.get(k) != v}}, u.get('actor') or 'Procurement')
    return {'ok': True, 'policy_version': POLICY.version, 'effective_date': str(POLICY.effective_date)}


@app.post('/api/policy/activate/{version}')
def activate_version(version: str):
    global POLICY
    con = db(); r = con.execute('SELECT policy_json FROM policy_versions WHERE version=?', (version,)).fetchone(); con.close()
    if not r: raise HTTPException(404, 'Unknown version')
    POLICY = Policy.from_dict(json.loads(r['policy_json'])); persist_policy(POLICY, active=True, source_filename='re-activated'); load_engine()
    record_action('reactivate_policy', {'version': version}, 'Procurement'); return {'ok': True, 'policy_version': version}


@app.get('/api/policy/history')
def policy_history():
    con = db(); rows = con.execute('SELECT version,effective_date,active,source_filename,created_at,policy_json FROM policy_versions ORDER BY effective_date,created_at').fetchall(); con.close()
    return {'rows': [{**{k: r[k] for k in ('version', 'effective_date', 'active', 'source_filename', 'created_at')}, 'rules': len(json.loads(r['policy_json']).get('rules', []))} for r in rows]}


# ---------------- chat ----------------
def _chat_state(sid):
    con = db(); r = con.execute('SELECT state_json FROM chat_sessions WHERE session_id=?', (sid,)).fetchone(); con.close()
    return json.loads(r['state_json']) if r else {}


def _save_chat_state(sid, state):
    con = db(); con.execute('INSERT OR REPLACE INTO chat_sessions(session_id,state_json,updated_at) VALUES(?,?,?)', (sid, json.dumps(state, default=str), now())); con.commit(); con.close()


def human_decision(d):
    if d['decision'] == 'PO':
        return f"PO via {d['route']} to supplier {d['supplier_id']} at ₹{float(d['unit_price']):,.2f}/unit (requirement value ₹{float(d['group_value_inr']):,.0f})."
    s = f" Suggested suppliers: {d['suggested_supplier_ids'].replace(';', ', ')}." if d['suggested_supplier_ids'] else ' The buyer will find suppliers.'
    return f"Sourcing request ({d['route']}), requirement value ₹{float(d['group_value_inr']):,.0f}.{s}"


@app.get('/api/chat/prs')
def chat_prs():
    con = db(); rows = con.execute('SELECT * FROM chat_prs ORDER BY id').fetchall(); con.close()
    return {'rows': [dict(r) for r in rows]}


@app.post('/api/chat')
async def chat(payload: dict):
    sid = str(payload.get('session_id', 'default')); text = str(payload.get('message', ''))
    if not data_loaded(): return {'reply': 'The procurement data is not loaded yet. Ask Procurement to import the task kit (Policy tab).', 'state': {}}
    state = _chat_state(sid)
    uoms = set(ENGINE.data['items'].uom.astype(str).str.lower())
    reply, state, create = chatbot.step(state, text, ENGINE, pd.Timestamp.now().date(), uoms)
    decision = None
    if create:
        row = create['row']; con = db(); n = (con.execute('SELECT COALESCE(MAX(id),0)+1 AS n FROM chat_prs').fetchone()['n']); con.close()
        row.update({'pr_no': f"CHAT-{pd.Timestamp.now():%Y%m%d}-{n:04d}", 'pr_line': 1, 'created_at': str(pd.Timestamp.now().floor('min'))})
        # evaluate together with earlier chat PRs so splitting a need across chats is still clubbed (policy 2.1)
        con = db(); prev = [json.loads(r['payload_json']) for r in con.execute('SELECT payload_json FROM chat_prs ORDER BY id DESC LIMIT 300')]; con.close()
        out = ENGINE.process(pd.DataFrame(prev[::-1] + [row])); decision = clean_rows(out[out.pr_no == row['pr_no']])[0]
        con = db(); con.execute('INSERT INTO chat_prs(pr_no,pr_line,session_id,created_at,payload_json,decision_json) VALUES(?,?,?,?,?,?)',
                                (row['pr_no'], 1, sid, row['created_at'], json.dumps(row, default=str), json.dumps(decision, default=str))); con.commit(); con.close()
        record_action('create_chat_pr', {'pr_no': row['pr_no'], 'session_id': sid, 'decision': decision}, payload.get('user') or 'Requester')
        state['last_pr'] = row['pr_no']
        reply = f"PR {row['pr_no']} created. Decision: {human_decision(decision)} Policy v{decision['policy_version']}, clause {decision['policy_clause']}."
    _save_chat_state(sid, state)
    return {'reply': reply, 'state': {k: v for k, v in state.items() if k not in ('choices',)}, 'decision': decision}


@app.get('/api/submission/status')
def submission_status():
    return {'files': sorted(f for f in os.listdir(SUBMISSION) if f.endswith('.csv')), 'required': ['run1.csv', 'run2.csv', 'preferred.csv']}
