"""Chat intake state machine. Pure logic (no web/DB): step() takes the session state + message and returns
(reply, new_state, create_request|None). Stages: collect -> item_choice -> estimate -> confirm -> done."""
from __future__ import annotations
import re
from datetime import date, datetime, timedelta
from typing import Optional, Tuple

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DAYS = {d: i for i, d in enumerate(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}
TRIGGER = r"(?:hi|hello|hey)?[\s,!.]*(?:(?:i|we)\s+)?(?:would like\s+to\s+(?:order|buy|get)|need|needs|require[d]?|want|please\s+(?:get|arrange|order|raise)|raise\s+a\s+pr\s+for|get|order|buy|arrange)\b"
CONFIRM = {"confirm", "yes", "y", "yes confirm", "confirm it", "ok", "okay", "yes please", "correct", "go ahead", "proceed", "confirmed"}
CANCEL = {"no", "cancel", "stop", "reset", "start over", "new request", "restart", "discard"}
THANKS = {"thanks", "thank you", "thx", "ty", "great", "ok thanks", "thanks!"}
UNITS = r"(?:units?|pcs?|pieces?|nos|no\.|numbers?|kgs?|kilograms?|ltrs?|litres?|liters?|mtrs?|metres?|meters?|jobs?|sets?|boxes|box|bkt|buckets?|rolls?|rol|pairs?|reams?|packs?|cartons?)"


def _mk(y, m, d) -> Optional[date]:
    try: return date(y, m, d)
    except ValueError: return None


def parse_date(text: str, today: date) -> Tuple[Optional[date], Optional[Tuple[int, int]]]:
    """Return (date, (start,end) span in text) for the first date expression found."""
    t = text
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m: return _mk(int(m[1]), int(m[2]), int(m[3])), m.span()
    m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b", t)
    if m:
        y = int(m[3]); y += 2000 if y < 100 else 0
        return _mk(y, int(m[2]), int(m[1])), m.span()
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:,?\s+(\d{4}))?", t, re.I)
    if m:
        y = int(m[3]) if m[3] else today.year
        d = _mk(y, MONTHS[m[2].lower()[:3]], int(m[1]))
        if d and not m[3] and d < today - timedelta(days=30): d = _mk(y + 1, d.month, d.day)
        return d, m.span()
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b", t, re.I)
    if m:
        y = int(m[3]) if m[3] else today.year
        d = _mk(y, MONTHS[m[1].lower()[:3]], int(m[2]))
        if d and not m[3] and d < today - timedelta(days=30): d = _mk(y + 1, d.month, d.day)
        return d, m.span()
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})\b", t)
    if m:
        d = _mk(today.year, int(m[2]), int(m[1]))
        if d and d < today - timedelta(days=30): d = _mk(today.year + 1, d.month, d.day)
        return d, m.span()
    low = t.lower()
    m = re.search(r"\bday after tomorrow\b", low)
    if m: return today + timedelta(days=2), m.span()
    m = re.search(r"\btomorrow\b", low)
    if m: return today + timedelta(days=1), m.span()
    m = re.search(r"\btoday\b", low)
    if m: return today, m.span()
    m = re.search(r"\bin\s+(\d{1,3})\s+days?\b", low)
    if m: return today + timedelta(days=int(m[1])), m.span()
    m = re.search(r"\bnext\s+week\b", low)
    if m: return today + timedelta(days=7), m.span()
    m = re.search(r"\b(next|this|coming|upcoming)?\s*(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", low)
    if m:
        wd = DAYS[m[2]]
        if m[1] == "next":   # the named weekday of NEXT calendar week
            monday_next = today + timedelta(days=7 - today.weekday())
            return monday_next + timedelta(days=wd), m.span()
        delta = (wd - today.weekday()) % 7 or 7
        return today + timedelta(days=delta), m.span()
    return None, None


def _strip_span(t: str, span, lead=r"(?:by|before|on|until|till|latest by|needed by|need by|due)?\s*") -> str:
    if not span: return t
    a, b = span
    pre = re.search(r"(?:\b(?:by|before|on|until|till|latest by|needed by|need by|due)\s*)$", t[:a], re.I)
    if pre: a = pre.start()
    return (t[:a] + " " + t[b:]).strip()


def parse_quantity(t: str, uoms) -> Tuple[Optional[float], Optional[Tuple[int, int]]]:
    unit = UNITS if not uoms else "(?:" + "|".join(sorted({re.escape(u.lower()) for u in uoms}, key=len, reverse=True)) + "|" + UNITS[3:-1] + ")"
    for pat in (r"(?:qty|quantity)\s*(?:is|=|:)?\s*(\d[\d,]*(?:\.\d+)?)", r"(?:make it|change (?:it )?to|update (?:it )?to)\s*(\d[\d,]*(?:\.\d+)?)",
                r"(?<![\w.x/-])(\d[\d,]*(?:\.\d+)?)\s*" + unit + r"\b",
                r"\b(?:need|needs|require|required|want|get|order|buy|arrange|raise|send)\s+(?:me\s+|us\s+)?(?:a\s+pr\s+for\s+)?(\d[\d,]*(?:\.\d+)?)\b(?!\s*[x/-]\s*\d)",
                r"^\s*(?:hi[, ]*)?(\d[\d,]*(?:\.\d+)?)\s+(?=[a-zA-Z])"):
        m = re.search(pat, t, re.I)
        if m: return float(m[1].replace(",", "")), m.span(1) if pat.startswith(r"(?<![") is False and False else (m.start(1), m.end(0) if re.search(unit, t[m.end(1):m.end(0)], re.I) else m.end(1))
    return None, None


def clean_description(t: str, aliases=None) -> str:
    if aliases:
        al = "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))
        t = re.sub(r"\b(?:for|at|in|from)?\s*(?:the\s+)?(?:" + al + r")\b(?:\s+plant)?", " ", t, flags=re.I)
    t = re.sub(r"\b(?:emergency|urgent(?:ly)?|asap|immediately|breakdown|normal|routine)\b", " ", t, flags=re.I)
    t = re.sub(r"^\W*" + TRIGGER, " ", t.strip(), flags=re.I)
    t = re.sub(r"\b(?:please|kindly|thanks|thank you)\b", " ", t, flags=re.I)
    t = re.sub(r"[\s,.;:!?-]+$", "", re.sub(r"^[\s,.;:!?-]+", "", t))
    t = re.sub(r"^(?:of|for|a|an|the|me|us)\s+", "", t.strip(), flags=re.I)
    t = re.sub(r"\s+(?:for|at|by|on|in)$", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


def parse_estimate(t: str) -> Optional[float]:
    m = re.search(r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k|lakh|lac|crore|cr)?\b", t, re.I)
    if not m: return None
    v = float(m[1].replace(",", ""))
    u = (m[2] or "").lower()
    return v * (1e3 if u == "k" else 1e5 if u in ("lakh", "lac") else 1e7 if u in ("crore", "cr") else 1)


def _fmt_date(d: str) -> str:
    return datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b %Y")


def summary(s: dict, engine) -> str:
    what = f"{s['quantity']:g} × {s['description']}"
    if s.get("item_code") and s["item_code"] != "NEW":
        what = f"{s['quantity']:g} × {engine.data['items'].set_index('item_code').loc[s['item_code'], 'description']} ({s['item_code']})"
    elif s.get("item_code") == "NEW":
        what += f" (new item, estimate ₹{s['estimated_unit_price']:,.2f}/unit)"
    return (f"Please confirm: {what} for {engine.plant_label(s['plant'])}, need-by {_fmt_date(s['need_by_date'])}, "
            f"{s.get('urgency', 'Normal')} urgency. Reply CONFIRM to raise the PR, or tell me what to change.")


def _resolve(s: dict, engine):
    """After all fields are known: resolve item; returns reply if a question is needed."""
    if s.get("resolved_for") != s["description"]:
        s["resolved_for"] = s["description"]; s.pop("item_code", None); s.pop("estimated_unit_price", None); s.pop("force_new", None)
    if "item_code" not in s:
        d = engine.match_detail({"description": s["description"]})
        if d["code"] != "NEW" and not d["ambiguous"]:
            s["item_code"] = d["code"]
        elif d["candidates"] and not s.get("force_new"):
            s["stage"] = "item_choice"; s["choices"] = [c["code"] for c in d["candidates"]]
            lines = "; ".join(f"{i}) {c['code']} {c['description']}" for i, c in enumerate(d["candidates"], 1))
            return (f"I couldn't match '{s['description']}' exactly. Closest items: {lines}. "
                    "Reply with the number or item code, or say NEW if it is not in the catalogue.")
        else:
            s["item_code"] = "NEW"
    if s["item_code"] == "NEW" and not s.get("estimated_unit_price"):
        s["stage"] = "estimate"
        return f"'{s['description']}' is not in the item master, so it will go to sourcing as a new item. What is your estimated unit price (INR per unit)?"
    s["stage"] = "confirm"
    return summary(s, engine)


def step(state: dict, text: str, engine, today: date, uoms=()) -> Tuple[str, dict, Optional[dict]]:
    s = dict(state or {})
    t, low = text.strip(), text.strip().lower().rstrip(".!")
    if not t: return "Tell me what you need, e.g. “Need 200 nitrile gloves for <plant> by 20/11/2026”.", s, None
    if low in CANCEL: return "Okay, starting a new purchase request. What do you need?", {}, None
    stage = s.get("stage", "collect")
    if low in CONFIRM and stage != "confirm":
        if stage == "done": return "That request is already confirmed and created. Tell me what else you need for a new PR.", s, None
        return "There is nothing to confirm yet. What do you need?", s, None
    if stage == "done":
        if low in THANKS: return "You're welcome! Tell me if you need another item.", s, None
        s, stage = {}, "collect"
    if stage in ("collect", "confirm") and re.match(r"^\W*" + TRIGGER, t, re.I) and (s.get("description") or s.get("quantity")):
        s, stage = {}, "collect"          # new request: nothing from the old one may leak
    if stage == "confirm" and low in CONFIRM:
        s["stage"] = "done"
        return "", s, {"row": {"plant": s["plant"], "item_code": "" if s["item_code"] == "NEW" else s["item_code"],
                               "description": s["description"], "quantity": s["quantity"], "need_by_date": s["need_by_date"],
                               "urgency": s.get("urgency", "Normal"),
                               "estimated_unit_price": s.get("estimated_unit_price", "") if s["item_code"] == "NEW" else ""}}
    if stage == "item_choice":
        code = None
        m = re.search(r"\bITM-\d+\b", t, re.I)
        if m and m[0].upper() in s.get("choices", []): code = m[0].upper()
        elif re.fullmatch(r"\s*[1-9]\s*", t) and int(t) <= len(s.get("choices", [])): code = s["choices"][int(t) - 1]
        if code: s["item_code"] = code; s.pop("choices", None); s["stage"] = "confirm"; return summary(s, engine), s, None
        if low in {"new", "none", "not listed", "other"}:
            s["force_new"] = True; s.pop("choices", None); s["item_code"] = "NEW"
            return _resolve(s, engine), s, None
        s["stage"] = "collect"; stage = "collect"; s.pop("choices", None); s.pop("resolved_for", None)   # treat as a new description
        s["description_pending"] = True
    if stage == "estimate":
        v = parse_estimate(t)
        if v and v > 0:
            s["estimated_unit_price"] = v
            return _resolve(s, engine), s, None
        return "Please send the estimated price per unit in INR, for example 1200.", s, None

    # ----- parse fields from the message (any order, any number per message) -----
    rest, got = t, []
    d, span = parse_date(rest, today)
    if d:
        if d < today:
            return f"That date ({d:%d %b %Y}) is in the past. What is the need-by date?", s, None
        s["need_by_date"] = str(d); got.append("date"); rest = _strip_span(rest, span)
    q, qspan = parse_quantity(rest, uoms)
    if q is not None and q > 0:
        s["quantity"] = q; got.append("qty")
        rest = (rest[:qspan[0]] + " " + rest[qspan[1]:]) if qspan else rest
    elif q is not None:
        return "Quantity must be more than zero. How many do you need?", s, None
    elif s.get("awaiting") == "quantity" and re.fullmatch(r"\s*\d[\d,]*(?:\.\d+)?\s*", t):
        s["quantity"] = float(t.replace(",", "")); got.append("qty"); rest = ""
    aliases = engine.plant_aliases()
    hit = [aliases[a] for a in aliases if re.search(r"\b" + re.escape(a) + r"\b", rest, re.I)]
    if len(set(hit)) == 1: s["plant"] = hit[0]; got.append("plant")
    elif len(set(hit)) > 1: return "That mentions more than one plant. A PR is raised for one plant at a time; which one first?", s, None
    if re.search(r"\b(emergency|urgent|urgently|asap|immediately|breakdown)\b", rest, re.I): s["urgency"] = "Emergency"; got.append("urgency")
    elif re.search(r"\b(normal|routine)\b", rest, re.I): s["urgency"] = "Normal"; got.append("urgency")
    desc = clean_description(rest, aliases)
    desc = re.sub(r"\b(?:qty|quantity)\b", " ", desc, flags=re.I).strip()
    expecting_item = s.get("awaiting") == "description" or not s.get("description") or s.pop("description_pending", False) \
        or bool(re.search(r"\b(?:actually|instead|item is|change item)\b", low))
    if desc and len(re.sub(r"[^a-z]", "", desc.lower())) >= 2 and expecting_item:
        desc = re.sub(r"^(?:actually|instead|item is|change item(?: to)?)\s*[:,]?\s*", "", desc, flags=re.I)
        s["description"] = desc; got.append("item")
    if not got and stage == "confirm":
        return "I didn't catch a change. Reply CONFIRM to raise the PR, or tell me the quantity, plant, date or item to change.", s, None
    if not got and not any(k in s for k in ("quantity", "description")):
        return "I couldn't find an item in that. What do you need? For example: “Need 200 nitrile gloves for <plant> by 20/11/2026”.", s, None
    # ----- ask for whatever is still missing, one question at a time -----
    s["stage"] = "collect"
    for key, q_txt in (("description", "What item or service do you need?"), ("quantity", "How many do you need?"),
                       ("plant", "Which plant? " + " or ".join(engine.plant_label(c) for c in sorted(set(aliases.values()))) + "?"),
                       ("need_by_date", "By when do you need it? (e.g. 20/11/2026 or next Friday)")):
        if key not in s:
            s["awaiting"] = {"description": "description", "quantity": "quantity", "plant": "plant", "need_by_date": "need_by_date"}[key]
            pre = f"Got it. " if got else ""
            return pre + q_txt, s, None
    s.pop("awaiting", None)
    return _resolve(s, engine), s, None
