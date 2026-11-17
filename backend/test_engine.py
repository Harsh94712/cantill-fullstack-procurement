"""Run: python -m backend.test_engine   (or pytest backend/test_engine.py)
Part 1 uses a tiny synthetic dataset (always runs). Part 2 re-checks the task-kit worked examples when data/ is present."""
import os
import pandas as pd
from .engine import ProcurementEngine, Policy
from .policy_parse import parse_policy_text
from . import chat


def mini(policy=None, contracts=None, pdf=None):
    items = pd.DataFrame([["ITM-A", "Ball bearing 6205 2RS", "Bearings", "Material", 100.0, "NOS"],
                          ["ITM-B", "CNC machine tool", "Capital Equipment", "Capex", 1e6, "NOS"],
                          ["ITM-C", "Nitrile coated gloves", "Safety", "Material", 50.0, "NOS"]],
                         columns=["item_code", "description", "category", "item_type", "standard_price", "uom"])
    sup = pd.DataFrame([["S1", "Alpha", "Active", 90, 4.5, "Bearings;Safety;Capital Equipment"], ["S2", "Beta", "Blocked", 99, 5, "Bearings;Safety"],
                        ["S3", "Gamma", "Active", 80, 4.0, "Bearings;Safety;Capital Equipment"]], columns=["supplier_id", "name", "status", "on_time_pct", "quality_rating", "categories"])
    po = pd.DataFrame([["PO1", "ITM-A", "S1", "2026-09-01", 100.0], ["PO2", "ITM-A", "S2", "2026-10-01", 90.0], ["PO3", "ITM-C", "S1", "2026-10-01", 50.0]],
                      columns=["po_number", "item_code", "supplier_id", "po_date", "unit_price"])
    d = {"items": items, "suppliers": sup, "po": po, "plants": pd.DataFrame({"plant": ["P1", "P2"]}),
         "contracts": contracts if contracts is not None else pd.DataFrame(columns=["contract_id", "supplier_id", "start_date", "end_date", "value_cap", "consumed_value"]),
         "contract_lines": pd.DataFrame(columns=["contract_id", "item_code", "rate"])}
    return ProcurementEngine(d, policy or Policy(plants=["P1", "P2"]), pdf)


def pr(n, item, qty, plant="P1", created="2026-11-02 09:00", need="2026-11-20", urg="Normal", est=None, line=1, desc=""):
    return {"pr_no": n, "pr_line": line, "created_at": created, "plant": plant, "item_code": item, "description": desc or item, "quantity": qty,
            "need_by_date": need, "urgency": urg, "estimated_unit_price": est}


def run(e, rows): return e.process(pd.DataFrame(rows))


def test_spot_never_blocked_supplier_and_clubbing():
    o = run(mini(), [pr("1", "ITM-A", 10), pr("2", "ITM-A", 5, created="2026-11-10 10:00", need="2026-11-25")])
    assert set(o.group_id) == {o.group_id[0]} and set(o.route) == {"SPOT_PO"}
    assert set(o.supplier_id) == {"S1"}            # S2 (Blocked) has a more recent PO but can never be chosen
    assert (o.group_value_inr == 1350).all() and (o.unit_price == 100).all()   # valued at latest PO (3.1b: 90), PO placed at preferred supplier S1 ref price


def test_no_club_outside_windows_and_different_plant():
    o = run(mini(), [pr("1", "ITM-A", 1), pr("2", "ITM-A", 1, need="2026-12-01"), pr("3", "ITM-A", 1, plant="P2"), pr("4", "ITM-A", 1, created="2026-11-20 10:00")])
    assert o.group_id.nunique() == 4


def test_new_item_never_clubbed_no_suppliers():
    o = run(mini(), [pr("1", "", 2, est=500, desc="drone camera"), pr("2", "", 2, est=500, desc="drone camera")])
    assert (o.route == "NEW_ITEM_SOURCING").all() and o.group_id.nunique() == 2 and (o.suggested_supplier_ids == "").all()


def test_thresholds_and_policy_as_data():
    rows = [pr("1", "ITM-A", 2500)]                 # 2,50,000 -> RFQ under v5.0
    assert run(mini(), rows).route[0] == "RFQ"
    assert run(mini(Policy(plants=["P1", "P2"], po_limit=300000)), rows).route[0] == "SPOT_PO"
    assert run(mini(Policy(plants=["P1", "P2"], rfq_limit=200000)), rows).route[0] == "RFP"
    em = [pr("1", "ITM-A", 4000, urg="Emergency")]  # 4,00,000
    assert run(mini(), em).route[0] == "EMERGENCY_PO"
    assert run(mini(Policy(plants=["P1", "P2"], emergency_limit=300000)), em).route[0] == "RFQ"


def test_capex_and_extra_rule():
    assert run(mini(), [pr("1", "ITM-B", 1)]).route[0] == "CAPEX_SOURCING"
    p = Policy(plants=["P1", "P2"], rules=[{"id": "r", "when": {"plant": "P2", "category": "Safety"}, "route": "RFQ"}])
    assert run(mini(p), [pr("1", "ITM-C", 1, plant="P2")]).route[0] == "RFQ"
    assert run(mini(p), [pr("1", "ITM-C", 1, plant="P1")]).route[0] == "SPOT_PO"


def test_contract_ceiling_in_requirement_order_and_pdf_contract():
    cr = pd.DataFrame([["RC1", "S1", "2026-01-01", "2026-12-31", 1000, 0]], columns=["contract_id", "supplier_id", "start_date", "end_date", "value_cap", "consumed_value"])
    e = mini(contracts=cr)
    e.data["contract_lines"] = pd.DataFrame([["RC1", "ITM-A", 40.0]], columns=["contract_id", "item_code", "rate"])
    o = run(e, [pr("1", "ITM-A", 20), pr("2", "ITM-A", 20, plant="P2", created="2026-11-03 09:00")])   # 800 uses ceiling; second 800 does not fit
    assert list(o.route) == ["CONTRACT_PO", "SPOT_PO"]
    pdf = {"RC9": {"contract_id": "RC9", "supplier_id": "S3", "start_date": "2026-11-01", "end_date": "2027-01-01", "value_cap": 1e6, "lines": [{"item_code": "ITM-A", "rate": 30.0}]}}
    assert run(mini(pdf=pdf), [pr("1", "ITM-A", 10)]).supplier_id[0] == "S3"


def test_not_processed_rows():
    o = run(mini(), [pr("1", "ITM-A", 0), pr("2", "ITM-A", 1, plant="ZZ"), pr("3", "ITM-A", 1, need="not-a-date"), pr("4", "ITM-A", 1), pr("4", "ITM-A", 1)])
    assert list(o.route) == ["NOT_PROCESSED"] * 3 + ["SPOT_PO", "NOT_PROCESSED"]


def test_consolidation_window():
    pol = Policy(plants=["P1", "P2"], consolidation_hours=2)
    o = run(mini(pol), [pr("1", "ITM-A", 1), pr("2", "ITM-C", 1, created="2026-11-02 10:00"), pr("3", "ITM-A", 1, plant="P1", created="2026-11-02 13:00", need="2026-12-30")])
    assert o.po_id[0] == o.po_id[1] != o.po_id[2]


def test_matcher():
    e = mini()
    assert e.match_detail({"description": "brg 6205 2rs for maintenance"})["code"] == "ITM-A"
    assert e.match_detail({"description": "laptop bags"})["code"] == "NEW"
    assert e.match_detail({"description": "gloves"})["code"] == "NEW"            # too vague: chat asks which item


def test_policy_parser_and_isodate():
    r = parse_policy_text("Version 5.1 effective 2026-12-01. 5.3 Emergency, value ≤ ₹3,00,000, and the item\n5.4 Value below ₹3 lakh, and", Policy())
    assert r["values"]["effective_date"] == "2026-12-01" and r["values"]["emergency_limit"] == 300000 and r["values"]["po_limit"] == 300000
    assert parse_policy_text("nothing useful here", Policy())["warnings"]


def test_chat_no_leak_and_dates():
    from datetime import date
    e, t, s = mini(), date(2026, 11, 17), {}
    for m in ["I need 200 nitrile gloves", "for P1", "by 20/11/2026"]:
        r, s, c = chat.step(s, m, e, t)
    assert s["quantity"] == 200 and s["need_by_date"] == "2026-11-20" and s["stage"] == "confirm"
    s["plant"] = "P1"; r, s, c = chat.step(s, "confirm", e, t); assert c and s["stage"] == "done"
    r, s, c = chat.step(s, "Need 10 drone cameras for P2 by 25 Nov 2026", e, t)
    assert s["quantity"] == 10 and s["stage"] == "estimate" and s["need_by_date"] == "2026-11-25"
    r, s, c = chat.step(s, "50000", e, t); assert s["quantity"] == 10 and s["estimated_unit_price"] == 50000
    assert chat.parse_date("next Friday", t)[0] == date(2026, 11, 27) and chat.parse_date("tomorrow", t)[0] == date(2026, 11, 18)


def test_worked_examples_when_kit_present():
    d = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
    if not os.path.exists(os.path.join(d, "sample_pr_batch.csv")): return print("  (kit data not present: worked-example check skipped)")
    from . import main
    main.POLICY = main.Policy(); main.load_pdf_contracts(); main.load_engine(); out = main.ENGINE.process(pd.read_csv(os.path.join(d, "sample_pr_batch.csv"))).set_index("pr_no")
    def chk(pr_no, route, sup=None, val=None, sug=None):
        r = out.loc[pr_no]; r = r.iloc[0] if isinstance(r, pd.DataFrame) else r
        assert r.route == route, (pr_no, r.route); assert sup is None or r.supplier_id == sup, (pr_no, r.supplier_id)
        assert val is None or abs(float(r.group_value_inr) - val) < 1.5, (pr_no, r.group_value_inr); assert sug is None or r.suggested_supplier_ids == sug, (pr_no, r.suggested_supplier_ids)
    chk("PR-26-00146", "SPOT_PO", "S024", 126472); chk("PR-26-00045", "CONTRACT_PO", "S021", 26829); chk("PR-26-00004", "RFQ", None, 220256, "S014;S013;S016")
    chk("PR-26-00026", "RFQ", None, 220256); chk("PR-26-00034", "CONTRACT_PO", "S007", 32547); chk("PR-26-00031", "CONTRACT_PO", "S007", 23227)
    chk("PR-26-00106", "SPOT_PO", "S027", 156170); chk("PR-26-00088", "SPOT_PO", "S023", 97955); chk("PR-26-00059", "EMERGENCY_PO", "S014", 253184)
    chk("PR-26-00089", "RFQ", None, 336562, "S043;S044;S042"); chk("PR-26-00050", "RFQ", None, 80300, "S003;S002;S005")
    chk("PR-26-00100", "RFP", None, 2070780, "S020;S017;S019;S018"); chk("PR-26-00047", "CAPEX_SOURCING", None, 6036704, "S045;S047;S020;S048;S046")
    chk("PR-26-00170", "NEW_ITEM_SOURCING", None, 141100); chk("PR-26-00172", "NOT_PROCESSED"); chk("PR-26-00173", "NOT_PROCESSED")
    assert out.loc["PR-26-00031"].po_id == out.loc["PR-26-00034"].po_id            # worked example 14: one PO, four lines
    assert len(out) == 186 and out.route.isin(["NOT_PROCESSED"]).sum() == 2


if __name__ == "__main__":
    n = 0
    for k, f in sorted(globals().items()):
        if k.startswith("test_") and callable(f): f(); n += 1; print("ok", k)
    print(f"{n} tests passed")
