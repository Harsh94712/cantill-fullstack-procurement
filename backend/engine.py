"""Policy decision engine. Pure logic: no web, no DB. All thresholds, windows,
capex types and extra rules come from the Policy object (policy-as-data)."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
from rapidfuzz import fuzz

PO_ROUTES = {"CONTRACT_PO", "EMERGENCY_PO", "SPOT_PO"}
SOURCING_ROUTES = {"RFQ", "RFP", "CAPEX_SOURCING"}
RULE_ROUTES = PO_ROUTES | SOURCING_ROUTES


@dataclass
class Policy:
    version: str = "5.0"
    effective_date: date = date(2026, 4, 1)
    plants: List[str] = field(default_factory=lambda: ["PUN1", "CHK1"])
    club_days: int = 14
    needby_days: int = 7
    po_limit: float = 200000
    rfq_limit: float = 1000000
    emergency_limit: float = 500000
    preferred_days: int = 548
    history_days: int = 365
    consolidation_hours: int = 24
    capex_types: List[str] = field(default_factory=lambda: ["Capex"])
    rfq_suggest: int = 3
    rfp_suggest: int = 5
    capex_suggest: int = 5
    # Extra rules: [{"id","clause","description","stage":"before_contract|after_contract",
    #   "when":{plant,category,item_type,urgency,item_code,min_value,max_value,has_history},
    #   "route": one of RULE_ROUTES}]  -> evaluated in list order.
    rules: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"version": self.version, "effective_date": str(self.effective_date), "plants": list(self.plants),
                "club_days": self.club_days, "needby_days": self.needby_days, "po_limit": self.po_limit,
                "rfq_limit": self.rfq_limit, "emergency_limit": self.emergency_limit,
                "preferred_days": self.preferred_days, "history_days": self.history_days,
                "consolidation_hours": self.consolidation_hours, "capex_types": list(self.capex_types),
                "rfq_suggest": self.rfq_suggest, "rfp_suggest": self.rfp_suggest,
                "capex_suggest": self.capex_suggest, "rules": [dict(r) for r in self.rules]}

    @classmethod
    def from_dict(cls, d: dict) -> "Policy":
        b = cls()
        eff = pd.Timestamp(d.get("effective_date", b.effective_date)).date()
        return cls(version=str(d.get("version", b.version)), effective_date=eff,
                   plants=list(d.get("plants", b.plants)), club_days=int(d.get("club_days", b.club_days)),
                   needby_days=int(d.get("needby_days", b.needby_days)), po_limit=float(d.get("po_limit", b.po_limit)),
                   rfq_limit=float(d.get("rfq_limit", b.rfq_limit)),
                   emergency_limit=float(d.get("emergency_limit", b.emergency_limit)),
                   preferred_days=int(d.get("preferred_days", b.preferred_days)),
                   history_days=int(d.get("history_days", b.history_days)),
                   consolidation_hours=int(d.get("consolidation_hours", b.consolidation_hours)),
                   capex_types=list(d.get("capex_types", b.capex_types)),
                   rfq_suggest=int(d.get("rfq_suggest", b.rfq_suggest)), rfp_suggest=int(d.get("rfp_suggest", b.rfp_suggest)),
                   capex_suggest=int(d.get("capex_suggest", b.capex_suggest)),
                   rules=[r for r in (d.get("rules") or []) if isinstance(r, dict) and r.get("route") in RULE_ROUTES])


# ---------- item matching helpers ----------
SYN = {"brg": "bearing", "brng": "bearing", "hyd": "hydraulic", "pneu": "pneumatic", "pneum": "pneumatic",
       "rod": "electrode", "prox": "proximity", "elec": "electric", "alu": "aluminium", "aluminum": "aluminium",
       "ms": "ms", "ss": "ss", "mtr": "mtr", "ltr": "ltr"}
NOISE = {"for", "maintenance", "as", "per", "sample", "urgent", "urgently", "repeat", "please", "the", "a", "an",
         "of", "and", "required", "needed", "asap", "pcs", "nos", "unit", "units", "pc"}


def _toks(s) -> List[str]:
    out = []
    for x in re.findall(r"[a-z0-9]+", str(s or "").lower()):
        x = SYN.get(x, x)
        if x.isalpha() and len(x) > 3 and x.endswith("s") and not x.endswith("ss"):
            x = x[:-1]
        if x not in NOISE:
            out.append(x)
    return out


def _tok_match(q: str, itoks: List[str]) -> bool:
    if q in itoks:
        return True
    if q.isalpha() and len(q) >= 4:
        return any(i.isalpha() and len(i) >= 4 and fuzz.ratio(q, i) >= 88 for i in itoks)
    return False


class ProcurementEngine:
    def __init__(self, data: Dict[str, pd.DataFrame], policy: Optional[Policy] = None,
                 pdf_contracts: Optional[Dict[str, dict]] = None):
        self.data = data
        self.policy = policy or Policy()
        self.pdf_contracts = pdf_contracts or {}
        self._normalise()
        self._item_tokens = None

    def _normalise(self):
        for k, df in list(self.data.items()):
            if isinstance(df, pd.DataFrame):
                self.data[k] = df.copy()
        for col in ["created_at", "po_date", "start_date", "end_date", "need_by_date"]:
            for df in self.data.values():
                if isinstance(df, pd.DataFrame) and col in df.columns:
                    df[col] = pd.to_datetime(df[col], errors="coerce")
        cr = self.data.get("contracts", pd.DataFrame())
        reg_ids = set(cr.contract_id.astype(str)) if not cr.empty else set()
        # A PDF whose contract is already in the register must not shadow it with consumed=0.
        self.pdf_contracts = {k: v for k, v in self.pdf_contracts.items() if str(v.get("contract_id", k)) not in reg_ids}

    # ---------- plants (names come from data, nothing hardcoded) ----------
    def plant_aliases(self) -> Dict[str, str]:
        pl = self.data.get("plants", pd.DataFrame())
        codes = [str(c) for c in (pl.plant if not pl.empty else self.policy.plants)]
        tok: Dict[str, set] = {}
        for c in codes:
            tok.setdefault(c.lower(), set()).add(c)
        if not pl.empty and "name" in pl.columns:
            for r in pl.itertuples():
                for w in re.findall(r"[A-Za-z]{4,}", str(r.name)):
                    if w.lower() not in {"plant", "unit", "works", "factory"}:
                        tok.setdefault(w.lower(), set()).add(str(r.plant))
        return {k: next(iter(v)) for k, v in tok.items() if len(v) == 1}

    def plant_label(self, code) -> str:
        pl = self.data.get("plants", pd.DataFrame())
        if not pl.empty and "name" in pl.columns:
            x = pl[pl.plant.astype(str) == str(code)]
            if not x.empty: return f"{x.iloc[0]['name']} ({code})"
        return str(code)

    # ---------- item matching ----------
    def _tokens(self):
        if self._item_tokens is None:
            it = self.data["items"]
            self._item_tokens = [(str(r.item_code), str(r.description), _toks(r.description)) for r in it.itertuples()]
        return self._item_tokens

    def match_detail(self, row) -> dict:
        items = self.data["items"]
        code = str(row.get("item_code") or "").strip()
        if not items.empty and code and code.lower() not in {"nan", "none"} and code in set(items.item_code.astype(str)):
            return {"code": code, "score": 100.0, "candidates": [], "ambiguous": False}
        q = _toks(row.get("description"))
        if not q or items.empty:
            return {"code": "NEW", "score": 0.0, "candidates": [], "ambiguous": False}
        spec = [x for x in q if any(c.isdigit() for c in x)]
        exact, partial = [], []
        for ic, idesc, it in self._tokens():
            if not it:
                continue
            matched = [x for x in q if _tok_match(x, it)]
            covq, covi = len(matched) / len(q), len(matched) / len(it)
            spec_ok = all(s in matched for s in spec)
            score = round(100 * (0.65 * covq + 0.35 * min(covi, 1.0)), 1)
            if covq >= 0.999 and spec_ok and covi >= 0.5:
                exact.append((score, ic, idesc))
            elif matched and covq >= 0.5 and (not spec or spec_ok or covq >= 0.75):
                partial.append((score, ic, idesc))
        exact.sort(key=lambda x: (-x[0], x[1]))
        partial.sort(key=lambda x: (-x[0], x[1]))
        if exact:
            amb = len(exact) > 1 and exact[0][0] - exact[1][0] < 3
            cands = [{"code": c, "description": d, "score": s} for s, c, d in exact[:3]]
            return {"code": exact[0][1], "score": exact[0][0], "candidates": cands if amb else [], "ambiguous": amb}
        return {"code": "NEW", "score": 0.0, "ambiguous": False,
                "candidates": [{"code": c, "description": d, "score": s} for s, c, d in partial[:3]]}

    def _item_match(self, row) -> Tuple[str, float]:
        d = self.match_detail(row)
        return d["code"], d["score"]

    def match_items(self, pr: pd.DataFrame) -> pd.DataFrame:
        out = pr.copy()
        if out.empty:
            out["resolved_item_code"], out["match_score"] = [], []
            return out
        pairs = out.apply(self._item_match, axis=1, result_type="expand")
        out["resolved_item_code"], out["match_score"] = pairs[0], pairs[1]
        return out

    # ---------- supplier helpers ----------
    def _supplier_active(self, sid) -> bool:
        s = self.data["suppliers"]
        x = s[s.supplier_id.astype(str) == str(sid)]
        return (not x.empty) and str(x.iloc[0].status).strip().lower() == "active"

    def _history_active(self, item, asof, days=365):
        po = self.data["po"]
        if item == "NEW" or po.empty:
            return False
        x = po[(po.item_code == item) & (po.po_date < asof) & (po.po_date >= asof - pd.Timedelta(days=days))]
        return any(self._supplier_active(s) for s in x.supplier_id)

    def _contract_history(self, sid, item, asof):
        """Most recent contract (started on/before asof, even if expired) of supplier covering item."""
        cr, cl = self.data.get("contracts", pd.DataFrame()), self.data.get("contract_lines", pd.DataFrame())
        cands = []
        if not cr.empty and not cl.empty:
            c = cr[(cr.supplier_id.astype(str) == str(sid)) & (cr.start_date <= asof)]
            if not c.empty:
                x = cl[(cl.item_code.astype(str) == str(item)) & cl.contract_id.isin(c.contract_id)]
                for _, line in x.iterrows():
                    st = c[c.contract_id == line.contract_id].iloc[0].start_date
                    cands.append({"contract_id": str(line.contract_id), "rate": float(line.rate), "start": pd.Timestamp(st)})
        for c in self.pdf_contracts.values():
            if str(c.get("supplier_id")) == str(sid) and pd.Timestamp(c["start_date"]) <= asof:
                for line in c.get("lines", []):
                    if str(line.get("item_code")) == str(item):
                        cands.append({"contract_id": str(c["contract_id"]), "rate": float(line["rate"]),
                                      "start": pd.Timestamp(c["start_date"])})
        if not cands:
            return None
        return sorted(cands, key=lambda x: (x["start"], x["contract_id"]))[-1]

    def _preferred(self, item, asof, topn=5):
        if item == "NEW":
            return []
        sup = self.data["suppliers"]
        po = self.data["po"]
        active = sup[sup.status.astype(str).str.strip().str.lower() == "active"]
        active_ids = set(active.supplier_id.astype(str))
        start = asof - pd.Timedelta(days=self.policy.preferred_days)
        h = po[(po.item_code == item) & (po.po_date < asof) & (po.po_date >= start)] if not po.empty else pd.DataFrame()
        rows = []
        seen = set()
        if not h.empty:
            for sid, g in h.groupby("supplier_id"):
                sid = str(sid)
                if sid not in active_ids:
                    continue
                s = active[active.supplier_id.astype(str) == sid].iloc[0]
                last = g.sort_values(["po_date", "po_number"]).iloc[-1]
                ch = self._contract_history(sid, item, asof)
                count = len(g)
                age = max(0, (asof - last.po_date).days)
                rec = 1.0 if age <= 90 else (0.5 if age <= 365 else 0.0)
                rows.append(self._score_row(sid, s, count, rec, ch, float(last.unit_price), age,
                                            f"PO {last.po_number}"))
                seen.add(sid)
        for sid in sorted(active_ids - seen):
            ch = self._contract_history(sid, item, asof)
            if ch is None:
                continue
            s = active[active.supplier_id.astype(str) == sid].iloc[0]
            rows.append(self._score_row(sid, s, 0, 0.0, ch, float(ch["rate"]), None, f"contract {ch['contract_id']}"))
        rows.sort(key=lambda x: (-x["score"], x["supplier_id"]))
        return rows[:topn]

    @staticmethod
    def _score_row(sid, s, count, rec, ch, ref_price, age, ref_src):
        ot, q = float(s.on_time_pct), float(s.quality_rating)
        score = 0.35 * min(count / 7, 1) + 0.25 * rec + 0.15 * (1 if ch else 0) + 0.15 * ot / 100 + 0.10 * q / 5
        why = (f"{count} PO(s) in lookback" + (f", last {age}d ago (recency {rec})" if age is not None else ", no PO in lookback")
               + f"; contract history {'yes' if ch else 'no'}; on-time {ot:g}%; quality {q:g}; score {score:.3f}; ref price from {ref_src}")
        return {"supplier_id": sid, "score": score, "reference_price": ref_price, "po_count": count, "recency": rec,
                "contract": bool(ch), "on_time": ot, "quality": q, "why": why, "ref_src": ref_src}

    def _applicable_contracts(self, item, asof):
        out = []
        cr, cl = self.data.get("contracts", pd.DataFrame()), self.data.get("contract_lines", pd.DataFrame())
        if not cr.empty and not cl.empty:
            for _, c in cr[(cr.start_date <= asof) & (cr.end_date >= asof)].iterrows():
                if not self._supplier_active(c.supplier_id):
                    continue
                for _, line in cl[(cl.contract_id == c.contract_id) & (cl.item_code.astype(str) == str(item))].iterrows():
                    out.append({**c.to_dict(), **line.to_dict(), "source": "register"})
        for c in self.pdf_contracts.values():
            if pd.Timestamp(c["start_date"]) <= asof <= pd.Timestamp(c["end_date"]) and self._supplier_active(c["supplier_id"]):
                for line in c.get("lines", []):
                    if str(line.get("item_code")) == str(item):
                        out.append({**{k: v for k, v in c.items() if k != "lines"}, **line, "source": "pdf"})
        return sorted(out, key=lambda x: (float(x["rate"]), str(x["contract_id"])))

    def _recent_po(self, item, asof):
        po = self.data["po"]
        x = po[(po.item_code == item) & (po.po_date < asof) & (po.po_date >= asof - pd.Timedelta(days=self.policy.history_days))]
        return None if x.empty else x.sort_values(["po_date", "po_number"]).iloc[-1]

    @staticmethod
    def _consumed(contract, consumed):
        return float(consumed.get(contract["contract_id"], contract.get("consumed_value", 0) or 0))

    def _price(self, item, asof, qty, consumed):
        """3.1 valuation + 4.2/4.3 contract choice -> (unit_price, usable_contract|None, note)."""
        contracts = self._applicable_contracts(item, asof)
        if contracts:
            for c in contracts:
                remaining = float(c["value_cap"]) - self._consumed(c, consumed)
                if remaining >= qty * float(c["rate"]):
                    return float(c["rate"]), c, (f"contract {c['contract_id']} ({c['supplier_id']}) rate {float(c['rate']):g}, "
                                                 f"ceiling left {remaining:,.0f}")
            low = contracts[0]
            ids = ", ".join(sorted({str(c['contract_id']) for c in contracts}))
            return float(low["rate"]), None, (f"all applicable contracts ({ids}) are out of ceiling; valued at lowest contract rate "
                                              f"{float(low['rate']):g} (3.1a)")
        r = self._recent_po(item, asof)
        if r is not None:
            return float(r.unit_price), None, f"valued at latest PO {r.po_number} ({r.po_date.date()}) price {float(r.unit_price):g} (3.1b)"
        row = self.data["items"].set_index("item_code").loc[item]
        return float(row.standard_price), None, f"no PO in {self.policy.history_days}d; valued at item-master standard price {float(row.standard_price):g} (3.1c)"

    # ---------- clubbing ----------
    def club(self, df):
        df = df.sort_values(["created_at", "pr_no", "pr_line"], kind="mergesort").copy()
        firsts: Dict[Tuple[str, str], List[Tuple[str, Any]]] = {}
        n = 0
        gids = {}
        for idx, r in df.iterrows():
            key = (r.resolved_item_code, str(r.plant))
            assigned = None
            if r.resolved_item_code != "NEW":
                for gid, f in firsts.get(key, []):
                    # calendar-day difference (see DECISIONS.md)
                    if (r.created_at.normalize() - f.created_at.normalize()).days <= self.policy.club_days and \
                            abs((r.need_by_date - f.need_by_date).days) <= self.policy.needby_days:
                        assigned = gid
                        break
            if assigned is None:
                n += 1
                assigned = f"G{n:04d}"
                if r.resolved_item_code != "NEW":
                    firsts.setdefault(key, []).append((assigned, r))
            gids[idx] = assigned
        df["group_id"] = pd.Series(gids)
        return df

    # ---------- rules ----------
    def _rule_hit(self, rule, ctx) -> bool:
        w = rule.get("when") or {}
        for k in ("plant", "category", "item_type", "urgency", "item_code"):
            v = w.get(k)
            if v not in (None, "", []):
                vals = [str(x).lower() for x in (v if isinstance(v, list) else [v])]
                if str(ctx[k]).lower() not in vals:
                    return False
        if w.get("min_value") is not None and ctx["value"] < float(w["min_value"]):
            return False
        if w.get("max_value") is not None and ctx["value"] >= float(w["max_value"]):
            return False
        if w.get("has_history") is not None and bool(w["has_history"]) != ctx["history"]:
            return False
        return True

    def _apply_rules(self, stage, ctx):
        for rule in self.policy.rules:
            if (rule.get("stage") or "before_contract") != stage or not self._rule_hit(rule, ctx):
                continue
            route = rule["route"]
            if route == "CONTRACT_PO" and not ctx["contract"]:
                continue
            if route in ("SPOT_PO", "EMERGENCY_PO") and not (ctx["history"] and ctx["preferred"]):
                continue
            return rule
        return None

    # ---------- main ----------
    def process(self, pr: pd.DataFrame) -> pd.DataFrame:
        df = pr.copy().reset_index(drop=True)
        for c, dv in [("item_code", ""), ("description", ""), ("plant", ""), ("urgency", "Normal"),
                      ("estimated_unit_price", np.nan), ("pr_line", 1)]:
            if c not in df.columns:
                df[c] = dv
        df["_ord"] = range(len(df))
        df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
        df["need_by_date"] = pd.to_datetime(df["need_by_date"], errors="coerce")
        df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
        df["estimated_unit_price"] = pd.to_numeric(df["estimated_unit_price"], errors="coerce")
        known_plants = set(map(str, self.policy.plants))
        if not self.data.get("plants", pd.DataFrame()).empty:
            known_plants |= set(self.data["plants"].plant.astype(str))
        bad = {}
        dup = df.duplicated(["pr_no", "pr_line"], keep="first")
        for i, r in df.iterrows():
            why = []
            if dup[i]: why.append("duplicate pr_no/pr_line")
            if pd.isna(r.created_at): why.append("missing/invalid created_at")
            if pd.isna(r.need_by_date): why.append("missing/invalid need_by_date")
            if pd.isna(r.quantity) or r.quantity <= 0: why.append("missing/non-positive quantity")
            if str(r.plant) not in known_plants: why.append(f"unknown plant '{r.plant}'")
            if why: bad[i] = "; ".join(why)
        good = df.drop(index=list(bad)).copy()
        out_rows = []
        if not good.empty:
            out_rows = self._process_valid(good)
        for i, why in bad.items():
            r = df.loc[i]
            out_rows.append({"_ord": r._ord, "pr_no": r.pr_no, "pr_line": r.pr_line, "resolved_item_code": "", "group_id": "",
                             "po_id": "", "decision": "", "route": "NOT_PROCESSED", "supplier_id": "", "unit_price": "",
                             "group_value_inr": "", "suggested_supplier_ids": "", "policy_version": self.policy.version,
                             "reason": f"NOT_PROCESSED: {why}", "policy_clause": "", "contract_id": ""})
        res = pd.DataFrame(out_rows).sort_values("_ord", kind="mergesort").drop(columns="_ord").reset_index(drop=True)
        return res

    def _process_valid(self, good):
        P = self.policy
        df = self.match_items(good)
        df = self.club(df)
        items = self.data["items"].set_index("item_code")
        consumed, decisions = {}, []
        for gid, g in df.groupby("group_id", sort=False):
            g = g.sort_values(["created_at", "pr_no", "pr_line"], kind="mergesort")
            first = g.iloc[0]
            asof, item, qty = first.created_at, first.resolved_item_code, float(g.quantity.sum())
            if item == "NEW":
                est = first.estimated_unit_price
                miss = pd.isna(est)
                value = qty * (0 if miss else float(est))
                decisions.append(self._sourcing(g, gid, value, "NEW_ITEM_SOURCING", "1.2", [],
                                                "new item (not in item master): always sourcing, never clubbed, no supplier suggestions; "
                                                + ("NO estimated unit price supplied, value 0" if miss else f"valued at qty x requester estimate {float(est):g}")))
                continue
            unit, contract, vnote = self._price(item, asof, qty, consumed)
            value = qty * unit
            irow = items.loc[item]
            preferred = self._preferred(item, asof, 5)
            history = self._history_active(item, asof, P.history_days)
            emergency = any(str(x).strip().lower() == "emergency" for x in g.urgency)
            ctx = {"plant": first.plant, "category": irow.category, "item_type": irow.item_type,
                   "urgency": "Emergency" if emergency else "Normal", "item_code": item, "value": value,
                   "history": history, "preferred": preferred, "contract": contract}
            d = None
            if str(irow.item_type) in P.capex_types:
                d = self._sourcing(g, gid, value, "CAPEX_SOURCING", "5.1",
                                   self._suggest(item, asof, P.capex_suggest, preferred),
                                   f"item type {irow.item_type}: capex always goes to sourcing, needs capex approval; {vnote}")
            if d is None:
                d = self._rule_decision(self._apply_rules("before_contract", ctx), ctx, g, gid, value, preferred, contract, vnote, asof)
            if d is None and contract:
                d = self._po(g, gid, "CONTRACT_PO", "5.2", contract["supplier_id"], float(contract["rate"]), value,
                             f"usable contract {contract['contract_id']} ({contract['source']}), {vnote}; lowest rate among usable contracts (4.3)",
                             str(contract["contract_id"]))
                consumed[contract["contract_id"]] = self._consumed(contract, consumed) + value
            if d is None:
                d = self._rule_decision(self._apply_rules("after_contract", ctx), ctx, g, gid, value, preferred, contract, vnote, asof)
            if d is None and emergency and value <= P.emergency_limit and history and preferred:
                d = self._po(g, gid, "EMERGENCY_PO", "5.3", preferred[0]["supplier_id"], preferred[0]["reference_price"], value,
                             f"emergency, value {value:,.0f} <= {P.emergency_limit:,.0f}, bought from Active supplier in last {P.history_days}d; "
                             f"1st preferred supplier {preferred[0]['supplier_id']} at ref price from {preferred[0]['ref_src']}; {vnote}")
            if d is None and value < P.po_limit and history and preferred:
                d = self._po(g, gid, "SPOT_PO", "5.4", preferred[0]["supplier_id"], preferred[0]["reference_price"], value,
                             f"value {value:,.0f} < {P.po_limit:,.0f}, bought from Active supplier in last {P.history_days}d; "
                             f"1st preferred supplier {preferred[0]['supplier_id']} at ref price from {preferred[0]['ref_src']}; {vnote}")
            if d is None and value < P.rfq_limit:
                why = "no purchase from an Active supplier in last %dd" % P.history_days if (value < P.po_limit and not history) else f"value {value:,.0f} >= {P.po_limit:,.0f}"
                d = self._sourcing(g, gid, value, "RFQ", "5.5", self._suggest(item, asof, P.rfq_suggest, preferred),
                                   f"{why}; below {P.rfq_limit:,.0f} so RFQ; {vnote}")
            if d is None:
                d = self._sourcing(g, gid, value, "RFP", "5.6", self._suggest(item, asof, P.rfp_suggest, preferred),
                                   f"value {value:,.0f} >= {P.rfq_limit:,.0f} so RFP; {vnote}")
            decisions.append(d)
        return self._consolidate(decisions)

    def _rule_decision(self, rule, ctx, g, gid, value, preferred, contract, vnote, asof):
        if not rule:
            return None
        route, clause = rule["route"], rule.get("clause") or f"rule {rule.get('id', '?')}"
        why = f"policy rule '{rule.get('description') or rule.get('id') or clause}' matched; {vnote}"
        item = ctx["item_code"]
        if route in SOURCING_ROUTES:
            n = {"RFQ": self.policy.rfq_suggest, "RFP": self.policy.rfp_suggest, "CAPEX_SOURCING": self.policy.capex_suggest}[route]
            return self._sourcing(g, gid, value, route, clause, self._suggest(item, asof, int(rule.get("suggest", n)), preferred), why)
        if route == "CONTRACT_PO":
            return self._po(g, gid, route, clause, contract["supplier_id"], float(contract["rate"]), value, why, str(contract["contract_id"]))
        return self._po(g, gid, route, clause, preferred[0]["supplier_id"], preferred[0]["reference_price"], value, why)

    def _suggest(self, item, asof, n, preferred):
        if item == "NEW":
            return []
        ids = [x["supplier_id"] for x in preferred]
        sup = self.data["suppliers"]
        cat = str(self.data["items"].set_index("item_code").loc[item, "category"])
        others = sup[(sup.status.astype(str).str.strip().str.lower() == "active") &
                     sup.categories.fillna("").str.contains(cat, regex=False)].copy()
        # best delivery AND quality record: equal-weight composite (reproduces worked examples 10 and 11)
        others["_c"] = others.on_time_pct.astype(float) / 100 + others.quality_rating.astype(float) / 5
        others = others.sort_values(["_c", "supplier_id"], ascending=[False, True])
        for sid in others.supplier_id.astype(str):
            if len(ids) >= n:
                break
            if sid not in ids:
                ids.append(sid)
        return ids[:n]

    def _po(self, g, gid, route, clause, supplier, price, value, reason, contract_id=""):
        return {"rows": g, "gid": gid, "decision": "PO", "route": route, "supplier": str(supplier), "price": float(price),
                "value": value, "clause": clause, "reason": reason, "contract_id": contract_id, "suggestions": []}

    def _sourcing(self, g, gid, value, route, clause, suggestions, reason):
        return {"rows": g, "gid": gid, "decision": "SOURCING_REQUEST", "route": route, "supplier": None, "price": None,
                "value": value, "clause": clause, "reason": reason, "suggestions": suggestions, "contract_id": ""}

    def _consolidate(self, decisions):
        seq, open_pos, out = 1, [], []
        for d in decisions:
            if d["decision"] != "PO":
                out.append(d)
                continue
            first = d["rows"].iloc[0]
            chosen = None
            for p in open_pos:
                delta = (first.created_at - p["first_time"]).total_seconds()
                if p["supplier"] == d["supplier"] and p["plant"] == first.plant and p["route"] == d["route"] \
                        and 0 <= delta <= self.policy.consolidation_hours * 3600:
                    chosen = p
                    break
            if not chosen:
                chosen = {"po_id": f"PO-{seq:04d}", "supplier": d["supplier"], "plant": first.plant, "route": d["route"],
                          "first_time": first.created_at}
                seq += 1
                open_pos.append(chosen)
            d["po_id"] = chosen["po_id"]
            out.append(d)
        rows = []
        for d in out:
            for _, r in d["rows"].iterrows():
                rows.append({"_ord": r._ord, "pr_no": r.pr_no, "pr_line": r.pr_line, "resolved_item_code": r.resolved_item_code,
                             "group_id": d["gid"], "po_id": d.get("po_id", ""), "decision": d["decision"], "route": d["route"],
                             "supplier_id": d.get("supplier") or "",
                             "unit_price": round(d["price"], 2) if d.get("price") is not None else "",
                             "group_value_inr": round(float(d["value"]), 2),
                             "suggested_supplier_ids": ";".join(d.get("suggestions", [])),
                             "policy_version": self.policy.version,
                             "reason": f"{d['clause']}: {d['reason']}", "policy_clause": d["clause"],
                             "contract_id": d.get("contract_id", "")})
        return rows

    def preferred_export(self, asof, topn: int = 3):
        rows = []
        ts = pd.Timestamp(asof)
        for item in self.data["items"].item_code:
            for i, x in enumerate(self._preferred(item, ts, topn), 1):
                rows.append({"item_code": item, "rank": i, "supplier_id": x["supplier_id"], "reference_price": x["reference_price"],
                             "score": round(x["score"], 4), "po_count": x["po_count"], "recency": x["recency"],
                             "contract": x["contract"], "on_time_pct": x["on_time"], "quality_rating": x["quality"], "why": x["why"]})
        return pd.DataFrame(rows, columns=["item_code", "rank", "supplier_id", "reference_price", "score", "po_count",
                                           "recency", "contract", "on_time_pct", "quality_rating", "why"])
