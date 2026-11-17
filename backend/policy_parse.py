"""Reads a policy / amendment document (text, PDF-extracted text or JSON) into Policy values + extra rules,
and describes the difference to the active policy in plain language."""
from __future__ import annotations
import json
import re
from typing import Dict, List, Optional
import pandas as pd
from .engine import Policy, RULE_ROUTES

AMT = r"(?:₹|INR|Rs\.?|Rupees)\s*([0-9][0-9,]*(?:\.\d+)?)\s*(lakhs?|lacs?|crores?|cr|k)?\b"
LABELS = {"po_limit": "Spot PO limit (direct PO below)", "rfq_limit": "RFQ limit (RFP at or above)",
          "emergency_limit": "Emergency PO limit (at or below)", "club_days": "Clubbing window (creation, calendar days)",
          "needby_days": "Need-by window (± days)", "consolidation_hours": "PO consolidation window (hours)",
          "preferred_days": "Preferred-supplier lookback (days)", "history_days": "Purchase-history lookback (days)",
          "rfq_suggest": "Suppliers suggested for RFQ", "rfp_suggest": "Suppliers suggested for RFP",
          "capex_suggest": "Suppliers suggested for Capex", "capex_types": "Item types treated as Capex",
          "effective_date": "Effective date", "version": "Version"}


def to_amount(num: str, unit: Optional[str]) -> float:
    v = float(num.replace(",", ""))
    u = (unit or "").lower()
    if u.startswith(("lakh", "lac")): v *= 1e5
    elif u.startswith(("crore", "cr")): v *= 1e7
    elif u == "k": v *= 1e3
    return v


def first_amount(s: str) -> Optional[float]:
    m = re.search(AMT, s, re.I)
    if m:
        return to_amount(m.group(1), m.group(2))
    m = re.search(r"\b([0-9]+(?:\.\d+)?)\s*(lakhs?|lacs?|crores?)\b", s, re.I)
    return to_amount(m.group(1), m.group(2)) if m else None


def parse_date(s: str) -> Optional[str]:
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"          # ISO is never day-first
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b", s)
    if m:
        try: return str(pd.to_datetime(f"{m.group(1)} {m.group(2)} {m.group(3)}", dayfirst=True).date())
        except Exception: pass
    m = re.search(r"\b([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", s)
    if m:
        try: return str(pd.to_datetime(f"{m.group(2)} {m.group(1)} {m.group(3)}", dayfirst=True).date())
        except Exception: pass
    m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b", s)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


def next_version(v: str) -> str:
    try:
        a, b = (str(v).split(".") + ["0"])[:2]
        return f"{a}.{int(b) + 1}"
    except Exception:
        return str(v) + ".1"


def _sentences(text: str) -> List[str]:
    out = []
    for line in re.split(r"\n+", text):
        out += [x.strip() for x in re.split(r"(?<=[.;])\s+(?=[A-Z0-9(])", line) if x.strip()]
    return out


def _num(s: str, pat: str) -> Optional[int]:
    m = re.search(pat, s, re.I)
    return int(m.group(1)) if m else None


def parse_policy_text(text: str, current: Policy, ctx: Optional[dict] = None) -> dict:
    ctx = ctx or {}
    cur = current.to_dict()
    base = dict(cur)
    found: Dict[str, bool] = {k: False for k in base}
    warnings: List[str] = []
    stripped = text.strip()
    rules: List[dict] = []
    source = "text"
    if stripped.startswith("{") or stripped.startswith("["):
        source = "json"
        try:
            obj = json.loads(stripped)
            if isinstance(obj, list):
                obj = {"rules": obj}
            for k, v in obj.items():
                if k in base:
                    base[k] = v; found[k] = True
            if "rules" in obj:
                rules = [r for r in obj["rules"] if isinstance(r, dict) and r.get("route") in RULE_ROUTES]
                dropped = len(obj["rules"]) - len(rules)
                if dropped: warnings.append(f"{dropped} rule(s) ignored: route must be one of {sorted(RULE_ROUTES)}")
            unknown = [k for k in obj if k not in base]
            if unknown: warnings.append("Unknown keys ignored: " + ", ".join(unknown))
        except Exception as e:
            warnings.append(f"Invalid JSON: {e}")
        return _finish(base, found, cur, rules, warnings, source, current, text)

    vm = re.findall(r"\b[Vv]ersion\s*[:\-]?\s*v?([0-9]+(?:\.[0-9]+)?)\b", text)
    if vm:
        base["version"] = sorted(vm, key=lambda x: tuple(map(int, x.split("."))))[-1]; found["version"] = True
    em = re.search(r"\bEffective(?:\s+(?:date|from|on))?\s*[:\-]?\s*([^\n·|;,]{6,30})", text, re.I)
    if em:
        d = parse_date(em.group(1))
        if d: base["effective_date"] = d; found["effective_date"] = True
        else: warnings.append("Effective date present but not readable; edit it before approving")

    blocks = {}
    ms = list(re.finditer(r"(?m)^\s*(\d+\.\d+)\s+", text))
    for i, m in enumerate(ms):
        blocks[m.group(1)] = text[m.start(): ms[i + 1].start() if i + 1 < len(ms) else len(text)]
    sents = _sentences(text)

    def setv(k, v):
        base[k] = v; found[k] = True

    # key: value style and clause blocks and keyword sentences, in that order of reliability
    for m in re.finditer(r"(?im)^\s*(spot po limit|po limit|direct po limit|rfq limit|rfp limit|emergency(?: po)? limit)\s*[:=\-]\s*(.+)$", text):
        a = first_amount(m.group(2)) or (to_amount(re.search(r"[0-9][0-9,]*(?:\.\d+)?", m.group(2)).group(0), None) if re.search(r"[0-9]", m.group(2)) else None)
        k = m.group(1).lower()
        if a is not None:
            setv("emergency_limit" if "emergency" in k else "rfq_limit" if ("rfq" in k or "rfp" in k) else "po_limit", a)
    for clause, key in (("5.3", "emergency_limit"), ("5.4", "po_limit"), ("5.5", "rfq_limit")):
        if not found[key] and clause in blocks:
            a = first_amount(blocks[clause].split("\n\n")[0]) 
            if a is not None: setv(key, a)
    for s in sents:
        low = s.lower()
        a = first_amount(s)
        if a is None or re.search(r"\bplant\b|category|item type", low) and not re.search(r"limit|threshold", low):
            continue
        mc = re.match(r"\s*(?:clause\s*)?(5\.[345])\b", s, re.I)
        raised = mc or re.search(r"limit|threshold|below|less than|at or below|up to|not exceed|raised|reduced|increased|changed|revised|set to|ceiling|cap|≤|<=|<", low)
        if not raised: continue
        ck = {"5.3": "emergency_limit", "5.4": "po_limit", "5.5": "rfq_limit"}.get(mc.group(1)) if mc else None
        if ck and not found[ck]: setv(ck, a)
        elif "emergency" in low and not found["emergency_limit"]: setv("emergency_limit", a)
        elif re.search(r"\brfp\b", low) and not found["rfq_limit"] and not re.search(r"\brfq\b", low.split("rfp")[0][-40:] if False else ""): setv("rfq_limit", a)
        elif re.search(r"\brfq\b|quotation|quotes", low) and not found["rfq_limit"]: setv("rfq_limit", a)
        elif re.search(r"spot|direct po|\bpo limit|purchase order limit", low) and not found["po_limit"]: setv("po_limit", a)

    def window(keys_pat, unit_pat, field):
        if found[field]: return
        for s in sents:
            if re.search(keys_pat, s, re.I):
                n = _num(s, r"within\s+(?:±\s*)?(\d+)\s*" + unit_pat) or _num(s, r"[±+/-]\s*(\d+)\s*" + unit_pat) or \
                    _num(s, r"(\d+)\s*(?:calendar\s*)?" + unit_pat)
                if n is not None: setv(field, n); return
    # clubbing: sentence about creation within N calendar days of first line
    if "2.2" in blocks:
        b = blocks["2.2"]
        m = re.search(r"created within\s+(\d+)\s+calendar days", b, re.I) or re.search(r"within\s+(\d+)\s+calendar days", b, re.I)
        if m: setv("club_days", int(m.group(1)))
        m = re.search(r"need-by date is within\s+(\d+)\s+days", b, re.I) or re.search(r"need-by[^.]{0,60}?(\d+)\s+days", b, re.I)
        if m: setv("needby_days", int(m.group(1)))
    if not found["club_days"]:
        for s in sents:
            if re.search(r"club|same requirement|first line", s, re.I) and not re.search(r"need-by only", s, re.I):
                n = _num(s, r"(?:created\s+)?within\s+(\d+)\s+(?:calendar\s+)?days") or _num(s, r"(\d+)[- ](?:calendar[- ])?day (?:clubbing|window)")
                if n: setv("club_days", n); break
    if not found["needby_days"]:
        for s in sents:
            if re.search(r"need[- ]by", s, re.I):
                n = _num(s, r"(?:within|±|\+/-)\s*(\d+)\s*days") or _num(s, r"(\d+)\s*days\s*\(?(?:either|each)")
                if n: setv("needby_days", n); break
    if "7.1" in blocks:
        n = _num(blocks["7.1"], r"within\s+(\d+)\s+hours")
        if n: setv("consolidation_hours", n)
    if not found["consolidation_hours"]:
        for s in sents:
            if re.search(r"consolidat|combined into one PO|same PO|one PO", s, re.I):
                n = _num(s, r"(\d+)\s*hours?")
                if n: setv("consolidation_hours", n); break
    if "6.1" in blocks:
        n = _num(blocks["6.1"], r"(\d+)\s+days")
        if n: setv("preferred_days", n)
    for s in sents:
        m = re.search(r"suggest\w*\s+(\d+)\s+suppliers", s, re.I)
        if m:
            n = int(m.group(1))
            if re.search(r"\bRFQ\b", s): setv("rfq_suggest", n)
            elif re.search(r"\bRFP\b", s): setv("rfp_suggest", n)
            elif re.search(r"capex", s, re.I): setv("capex_suggest", n)

    rules = _parse_rules(sents, ctx, warnings)
    return _finish(base, found, cur, rules, warnings, source, current, text)


def _parse_rules(sents, ctx, warnings) -> List[dict]:
    plants = [str(p) for p in ctx.get("plants", [])]
    cats = [str(c) for c in ctx.get("categories", [])]
    types = [str(t) for t in ctx.get("item_types", [])]
    rules = []
    for s in sents:
        low = s.lower()
        mroute = re.search(r"\b(CAPEX_SOURCING|EMERGENCY_PO|CONTRACT_PO|SPOT_PO|RFQ|RFP)\b", s) or \
                 (re.search(r"\b(spot po|rfq|rfp)\b", low) if False else None)
        if not mroute:
            continue
        if not re.search(r"\b(always|must|shall|go(?:es)?\s+to|sent\s+to|route[sd]?\s+to|should go|will go|use)\b", low):
            continue
        when = {}
        for p in plants:
            if re.search(r"\b" + re.escape(p) + r"\b", s, re.I): when["plant"] = p
        for c in cats:
            if re.search(r"\b" + re.escape(c) + r"\b", s, re.I): when["category"] = c
        if "category" not in when:        # "Safety" should match a category called "PPE & Safety"
            mc = re.search(r"categor(?:y|ies)\s*[:=]?\s*([A-Za-z&][A-Za-z& ]{2,40}?)(?=\s+(?:items?|goods|must|always|shall|should|go|will|are|is)\b|[,.;]|$)", s, re.I)
            if mc:
                hits = [c for c in cats if mc.group(1).strip().lower() in c.lower()]
                if len(hits) == 1: when["category"] = hits[0]
            if "category" not in when:
                for c in cats:
                    words = [w for w in re.findall(r"[A-Za-z]{4,}", c) if w.lower() not in ("and", "parts", "items")]
                    if any(re.search(r"\b" + re.escape(w) + r"\b", s, re.I) for w in words) and re.search(r"categor", low):
                        when["category"] = c; break
        for t in types:
            if re.search(r"item type\s*[:=]?\s*" + re.escape(t) + r"\b|\b" + re.escape(t) + r"\s+items?\b", s, re.I): when["item_type"] = t
        if re.search(r"\bemergency\b", low) and mroute.group(1) != "EMERGENCY_PO": when["urgency"] = "Emergency"
        m = re.search(r"(?:below|under|less than)\s+" + AMT, s, re.I)
        if m: when["max_value"] = to_amount(m.group(1), m.group(2))
        m = re.search(r"(?:above|over|at least|more than|exceeding)\s+" + AMT, s, re.I) or re.search(AMT + r"\s+or more", s, re.I)
        if m: when["min_value"] = to_amount(m.group(1), m.group(2))
        unresolved = [w for w, pat in (("category", r"categor"), ("plant", r"\bplant\b"), ("item type", r"item type")) if re.search(pat, low)
                      and w.replace(" ", "_") not in when and not (w == "plant" and "plant" in when)]
        if unresolved:
            warnings.append("Rule ignored (could not understand the " + "/".join(unresolved) + " condition): " + s.strip()[:140])
            continue
        if not when:
            continue
        stage = "after_contract" if re.search(r"after contract|unless a contract|if no contract|no usable contract", low) else "before_contract"
        rules.append({"id": f"R{len(rules) + 1}", "clause": "amendment rule " + str(len(rules) + 1),
                      "description": s.strip()[:200], "stage": stage, "when": when, "route": mroute.group(1)})
    return rules


def _fmt(v):
    if isinstance(v, float) and v.is_integer(): v = int(v)
    if isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) >= 1000: return f"₹{v:,.0f}"
    return str(v)


def describe_rule(r: dict) -> str:
    w = r.get("when") or {}
    conds = []
    for k, lab in (("plant", "plant"), ("category", "category"), ("item_type", "item type"), ("urgency", "urgency"), ("item_code", "item")):
        if w.get(k): conds.append(f"{lab} = {w[k]}")
    if w.get("min_value") is not None: conds.append(f"value ≥ ₹{float(w['min_value']):,.0f}")
    if w.get("max_value") is not None: conds.append(f"value < ₹{float(w['max_value']):,.0f}")
    if w.get("has_history") is not None: conds.append("has purchase history" if w["has_history"] else "no purchase history")
    where = "before contracts (rule 5.2)" if (r.get("stage") or "before_contract") == "before_contract" else "after contract check"
    return f"When {' and '.join(conds) or 'always'}: route to {r['route']} ({where})"


def _finish(base, found, cur, rules, warnings, source, current, text):
    if not found.get("version"):
        base["version"] = next_version(current.version)
        warnings.append(f"No version number found; suggested {base['version']} (editable before approval)")
    if rules:
        base["rules"] = list(cur.get("rules", [])) + [r for r in rules if r not in cur.get("rules", [])]
        found["rules"] = True
    changes = []
    for k in ("version", "effective_date", "po_limit", "rfq_limit", "emergency_limit", "club_days", "needby_days",
              "consolidation_hours", "preferred_days", "history_days", "rfq_suggest", "rfp_suggest", "capex_suggest", "capex_types"):
        if base.get(k) != cur.get(k):
            changes.append({"field": k, "label": LABELS[k], "old": _fmt(cur.get(k)), "new": _fmt(base.get(k)),
                            "text": f"{LABELS[k]}: {_fmt(cur.get(k))} → {_fmt(base.get(k))}"})
    old_rules = cur.get("rules", [])
    for r in base.get("rules", []):
        if r not in old_rules:
            changes.append({"field": "rules", "label": "New rule", "old": "-", "new": describe_rule(r), "text": "New rule: " + describe_rule(r)})
    if not any(c["field"] not in ("version", "effective_date") for c in changes):
        warnings.append("No rule or limit change could be read from this document. Check the text, or use the JSON format.")
    return {"values": base, "found": found, "changes": changes, "warnings": warnings, "source": source}
