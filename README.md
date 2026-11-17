# Corvo Procurement Assistant (Cantill.ai Full-Stack Challenge)

PR-to-PO assistant for Corvo Engineering: an **Employee Chat**, a **Buyer Workspace** and **Policy Management**, all driven by one
policy engine (`backend/engine.py`) that reads its thresholds, windows and extra rules from a versioned policy object, not from code.

## Run it

```bash
docker compose up --build        # http://localhost:8000
```
Without Docker: `pip install -r backend/requirements.txt && uvicorn backend.main:app --port 8000`.

**Loading the task data (no company data is committed to this repo)**
- Open **Policy Management → Import task-kit ZIP** and upload `Cantill_FullStack_Task_Kit_vf.zip`, **or**
- drop that ZIP into `seed/` (or set `TASK_KIT_ZIP=/path/kit.zip`): the app imports it by itself on start-up whenever no data is loaded.
  This is the easiest way to make a hosted deployment survive restarts.

All mutable state (task data, `app.db`, policy files) lives in `DATA_DIR` (default `./data`, mounted as a volume by `docker-compose.yml`),
so `docker compose down && up` keeps policy versions, runs, chat PRs and the audit log. Hosted: mount a persistent disk at `DATA_DIR`
or use the `seed/` ZIP so data is re-imported on every start (policy versions need the disk).

## What is where

| Area | How to use it | Code |
|---|---|---|
| Employee Chat | Free-text request, any order, multi-turn; asks only what is missing; confirm → PR + decision | `backend/chat.py`, `POST /api/chat` |
| Buyer Workspace | Upload batch / run sample, requirement cards with route, supplier, price, value, contract/policy basis, reason; **Approve** (creates PO/RFQ/RFP number) and **Override** (written reason mandatory, blocked suppliers refused); chat PRs; contracts (PDF upload or manual); preferred-supplier export; audit trail | `backend/main.py` |
| Policy Management | Upload policy/amendment (PDF, text, JSON) or describe the change; plain-language "what would change"; edit version/date; approve; history; re-activate | `backend/policy_parse.py` |

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/process-batch` | multipart `file` (PR batch CSV). Returns JSON `{run_id, policy_version, summary, rows}`; **CSV** with `?format=csv` or `Accept: text/csv`. `?run_name=run1\|run2` also writes `submission/run1.csv` / `run2.csv`. Uses the active policy |
| `GET /api/runs`, `/api/runs/{id}`, `/api/runs/{id}/export[?save_as=run1]` | saved runs, requirement view, CSV export |
| `GET /api/preferred?as_of=&top=`, `GET /api/preferred/export?as_of=&top=3&save=true` | preferred suppliers (JSON with explanation / required 4-column CSV) |
| `POST /api/policy`, `/api/policy/describe`, `/api/policy/approve`, `GET /api/policy/active`, `/api/policy/history`, `POST /api/policy/activate/{v}` | policy-as-data |
| `POST /api/requirements/approve`, `/api/requirements/override`, `GET /api/actions` | buyer workflow + audit trail |
| `GET/POST /api/contracts`, `POST /api/contracts/pdf` | contract register incl. PDF-only contracts |
| `POST /api/chat`, `GET /api/chat/prs` | chat |
| `POST /api/import-kit`, `GET /api/health` | data import / status |

CSV columns: `pr_no, pr_line, resolved_item_code, group_id, po_id, decision, route, supplier_id, unit_price, group_value_inr, suggested_supplier_ids, policy_version, reason`
(+ `policy_clause, contract_id` at the end). Routes: `CONTRACT_PO, EMERGENCY_PO, SPOT_PO, RFQ, RFP, CAPEX_SOURCING, NEW_ITEM_SOURCING, NOT_PROCESSED`.

## Policy as data: what changes without code, and what does not

Without code: version/effective date, spot / RFQ / emergency limits, clubbing window, need-by window, consolidation window, lookback days,
capex item types, number of suggested suppliers, and **extra rules** (`when` plant / category / item type / urgency / item / value range / purchase history → `route`,
evaluated before or after the contract check). Amendments are read from clause-numbered text (like the original policy), simple "limit is raised to ₹…" / "Label: INR …" wording,
`lakh`/`crore` amounts, or JSON. Anything the parser cannot read is shown as a warning, never guessed.
Needs code: a brand-new *kind* of condition or a different order of the built-in rule chain (capex → extra rules → contract → emergency → spot → RFQ → RFP), and the preferred-supplier score weights.
An approved version applies immediately to new batches and chats; the effective date is recorded and shown (see DECISIONS.md D11).

## Tests

`python -m backend.test_engine` (12 tests: synthetic data always, plus the task-kit worked examples when the kit is loaded in `data/`).

## AI disclosure

I used **ChatGPT (GPT-6)** and **Claude** for code review, bug finding, debugging assistance and drafting. I ran the engine against the task-kit data myself, compared every worked example, and the design decisions in `DECISIONS.md` are mine to explain.

## Repository hygiene

Git ignores `data/*` (task data, SQLite, policy JSON) and generated `submission/*.csv`. The Docker image contains only code and docs.
