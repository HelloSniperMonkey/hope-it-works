"""One runnable check for the rules, no API needed: the facts the extractor should return for
data/examples/overcharge, run through the engine, must give the worked example's answer, with quotes the grader can
find.   python harness/test_engine.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "grader")]
import docs as DOCS  # noqa: E402
import engine  # noqa: E402
import harness  # noqa: E402
from extract import Mailbox  # noqa: E402

BOX = HERE.parent / "data" / "examples" / "overcharge" / "docs"

MSA = {
    "contract_id": "TBX-US-EXP-E60", "contract_src": "1:3", "carrier_name": "Tarrowby Express Inc.",
    "carrier_party": "TBX-US", "shipper_name": "Quillmere Pharma Inc.", "shipper_party": "QMP-US", "currency": "USD",
    "signed_by_both": True, "dated": "2031-11-24", "effective_date": "2031-12-17", "effective_src": "3:20",
    "end_date": "2032-12-16", "end_src": "3:21", "auto_renews": False, "renewal_src": "3:22",
    "renewal_notice_days": None, "notice_deadline": None,
    "rate_periods": [{"name": "P1", "first_pickup": "2031-12-17", "last_pickup": "2032-06-16", "src": "8:3"},
                     {"name": "P2", "first_pickup": "2032-06-17", "last_pickup": "2032-12-16", "src": "8:4"}],
    "last_period_continues": True,
    "rates": [{"lane": "USE2-INE2", "band": b, "lo": lo, "hi": hi, "is_minimum_charge": m,
               "period_rates": [{"period": "P1", "rate": p1}, {"period": "P2", "rate": p2}], "src": f"8:{n}"}
              for n, b, lo, hi, m, p1, p2 in [(31, "minimum charge", None, None, True, "58.00", "58.00"),
                                              (32, "0-44.5 kg", "0", "44.5", False, "7.73", "7.93"),
                                              (33, "45-99.5 kg", "45", "99.5", False, "6.81", "6.99"),
                                              (34, "100-299.5 kg", "100", "299.5", False, "6.01", "6.16"),
                                              (35, "300+ kg", "300", None, False, "5.29", "5.43")]],
    "fuel": [{"month": "2031-12", "percent": "20.16", "src": "9:4"}, {"month": "2032-01", "percent": "22.00",
                                                                      "src": "9:5"}],
    "fuel_rule": {"applies_to": ["FRT"], "src": "3:36-37"},
    "accessorials": [{"code": "SEC", "name": "Security surcharge", "fee": "0.15", "currency": "USD", "unit": "per_kg",
                      "only_months": [], "src": "10:6"},
                     {"code": "DLV", "name": "Delivery fee", "fee": "121.81", "currency": "USD",
                      "unit": "per_shipment", "only_months": [], "src": "10:8"}],
    "charge_codes": [{"code": "FRT", "name": "Air freight", "aliases": ["Express freight", "Freight charge"],
                      "src": "10:14"},
                     {"code": "FUEL", "name": "Fuel surcharge", "aliases": ["FSC", "Fuel adj."], "src": "10:15"},
                     {"code": "SEC", "name": "Security surcharge", "aliases": ["Screening fee"], "src": "10:16"},
                     {"code": "DLV", "name": "Delivery fee", "aliases": ["Door delivery"], "src": "10:20"}],
    "fx": [], "fx_update": None,
    "authorised_persons": [{"name": "Yara Nairne", "title": "Pricing Manager", "party": "TBX-US",
                            "categories": ["rates"], "email": "yara.nairne@tarrowby.example", "src": "11:6"}],
    "clauses": {"pricing_date": {"text_src": "3:28"}, "delivery": {"only_if_carrier_delivers": True, "src": "4:6"},
                "precedence": {"notice_vs_amendment": "unranked", "src": "5:3-4", "unranked_src": "4:43-44"}},
    "referenced_documents": [],
}


def inv(no, date_, total, lines):
    return {"invoice_number": no, "number_src": "1:3", "document_type": "invoice", "invoice_date": date_,
            "due_date": None, "date_src": "1:4", "contract_id": "TBX-US-EXP-E60", "currency": "USD",
            "issuer": "Tarrowby Express Inc.", "marked_copy": "no", "subtotal": total, "taxes": [], "total": total,
            "total_src": "1:15",
            "lines": [{"awb": awb, "master_awb": None, "date": d, "date_label": "Pickup", "description": desc,
                       "lane": "USE2-INE2", "quantity": q, "unit": u, "rate": r, "rate_is_percent": pct,
                       "percent_base": base, "amount": a, "period": None, "late_fee": None, "src": f"1:{n}"}
                      for n, awb, d, desc, q, u, r, pct, base, a in lines]}


FACTS = {
    "batch_01/0002.eml#MSA-TBX-US-EXP-E60.pdf": ("contract", MSA),
    "batch_01/0003.eml#TX-US-2032-5394.pdf": ("invoice", inv("TX-US-2032-5394", "2032-01-02", "2095.29", [
        (9, "TE56915687", "2031-12-25", "Air freight", "241", "kg", "6.69", False, None, "1612.29"),
        (10, "TE56915687", "2031-12-25", "Fuel surcharge", None, None, "20.16", True, "1612.29", "325.04"),
        (11, "TE56915687", "2031-12-25", "Security surcharge", "241", "kg", "0.15", False, None, "36.15"),
        (12, "TE56915687", "2031-12-25", "Delivery fee", "1", "shpt", "121.81", False, None, "121.81")])),
    "batch_01/0004.eml#TX-US-2032-5397.pdf": ("invoice", inv("TX-US-2032-5397", "2032-01-26", "543.36", [
        (9, "TE68271803", "2032-01-18", "Air freight", "44", "kg", "7.73", False, None, "340.12"),
        (10, "TE68271803", "2032-01-18", "Fuel surcharge", None, None, "22.00", True, "340.12", "74.83"),
        (11, "TE68271803", "2032-01-18", "Security surcharge", "44", "kg", "0.15", False, None, "6.60"),
        (12, "TE68271803", "2032-01-18", "Delivery fee", "1", "shpt", "121.81", False, None, "121.81")])),
    "batch_01/0005.eml#tracking-2031-07-01.xlsx": ("tracking_export", {"rows": [
        {"awb": a, "event": e, "date": d, "station": s, "reason_code": None, "src": f"1:{n}"}
        for n, a, e, d, s in [(2, "TE56915687", "PICKED_UP", "2031-12-25", "USE2"),
                              (4, "TE56915687", "ARRIVED_FACILITY", "2031-12-29", "INE2"),
                              (6, "TE56915687", "DELIVERED", "2031-12-30", "INE2"),
                              (7, "TE68271803", "PICKED_UP", "2032-01-18", "USE2"),
                              (9, "TE68271803", "ARRIVED_FACILITY", "2032-01-21", "INE2"),
                              (11, "TE68271803", "DELIVERED", "2032-01-22", "INE2")]]}),
}


def build_state(state: Path) -> None:
    (state / "docs").mkdir(parents=True)
    for f in sorted(BOX.rglob("*.eml")):
        group = DOCS.read_email(f, f.parent.name)
        rec = {"meta": group[0].meta, "docs": []}
        for d in group:
            kind, facts = FACTS.get(d.ref, ("email" if d.ext == "eml" else "other", {}))
            facts = json.loads(json.dumps(facts))
            if kind == "contract":
                # This minimal fixture originally omitted the dispute recipient. Include the actual Schedule 1 row
                # so the autonomous positive control has the authority it claims to have.
                for n, row in enumerate(d.pages[10].splitlines(), 1):
                    if "billing disputes" in row.lower() and "TBX-US" in row:
                        address = re.search(r"[\w.+-]+@[\w.-]+", row)
                        assert address, row
                        facts["authorised_persons"].append({"name": "Disputes contact", "party": "TBX-US",
                                                           "categories": ["billing_disputes"],
                                                           "email": address[0], "src": f"11:{n}"})
            rec["docs"].append({"ref": d.ref, "kind": kind, "scan": False,
                                "facts": harness._resolve(facts, d.pages)})
        (state / "docs" / (group[0].ref.replace("/", "_") + ".json")).write_text(json.dumps(rec))


def write_state(state: Path, docs: list) -> None:
    """Hand-written facts as ingest would store them: (kind, facts, received date[, sender address])."""
    (state / "docs").mkdir(exist_ok=True)
    for i, (kind, facts, rec, *frm) in enumerate(docs):
        meta = {"email": f"b/{i}.eml", "received": rec + "T09:00:00Z", "from_addr": (frm or ["x@car.example"])[0],
                "from_name": "X"}
        (state / "docs" / f"{i}.json").write_text(json.dumps(
            {"meta": meta, "docs": [{"ref": f"b/{i}.eml#{i}.pdf", "kind": kind, "facts": facts}]}))


def review_check() -> None:
    """Fixes from the glossary review. An INR conflict reports its range in USD, and an unchanged key keeps its id
    from one report to the next (P1, F1). Late-fee month 2 billed on its own later invoice is caught (LF-1)."""
    contract = {"contract_id": "C-IN", "carrier_party": "CAR", "currency": "INR", "effective_date": "2040-01-01",
                "end_date": "2040-12-31", "rate_periods": [{"name": "P1", "first_pickup": "2040-01-01",
                                                            "last_pickup": "2040-12-31"}],
                "rates": [{"lane": "IN1-US1", "band": "100-299.5 kg", "lo": "100", "hi": "299.5",
                           "period_rates": [{"period": "P1", "rate": "80.00"}]}],
                "fuel": [{"month": "2040-03", "percent": "10"}], "fx": [{"month": "2040-03", "inr_per_usd": "80.00"}],
                "charge_codes": [{"code": "FRT", "name": "Air freight", "aliases": []},
                                 {"code": "FUEL", "name": "Fuel surcharge", "aliases": []}],
                "authorised_persons": [{"name": "Rita Rate", "party": "CAR", "categories": ["rates"],
                                        "email": "rita@car.example"}],
                "clauses": {"precedence": {"notice_vs_amendment": "unranked"}}}
    amd = {"contract_id": "C-IN", "signed_by_both": True, "effective_from": "2040-02-01",
           "rate_changes": [{"lane": "IN1-US1", "band": "100-299.5 kg", "lo": "100", "hi": "299.5", "rate": "70.00"}]}
    notice = {"rate_notices": [{"lane": "IN1-US1", "effective_from": "2040-02-15", "effective_to": "2040-06-30",
                                "written_by_name": "Rita Rate", "written_by_email": "rita@car.example",
                                "quoted_only": "no", "rows": [{"band": "100-299.5 kg", "lo": "100", "hi": "299.5",
                                                               "rate": "75.00"}]}]}
    inv = {"invoice_number": "IN-1", "invoice_date": "2040-03-20", "contract_id": "C-IN", "currency": "INR",
           "total": "17600.00", "lines": [
               {"awb": "A1", "lane": "IN1-US1", "date": "2040-03-10", "description": "Air freight", "quantity": "200",
                "rate": "80.00", "amount": "16000.00"},
               {"awb": "A1", "lane": "IN1-US1", "date": "2040-03-10", "description": "Fuel surcharge",
                "rate": "10", "rate_is_percent": True, "percent_base": "16000.00", "amount": "1600.00"}]}
    track = {"rows": [{"awb": "A1", "event": "PICKED_UP", "date": "2040-03-10"}]}
    lf = {"contract_id": "C-LF", "carrier_party": "CAR", "currency": "USD", "effective_date": "2039-01-01",
          "charge_codes": [{"code": "LATE", "name": "Late fee", "aliases": []}],
          "clauses": {"late_fee": {"percent_per_month": "1.86", "grace_days": 5, "days_per_month": 30}}}
    reg = {"rows": [{"invoice_number": "T-1", "due_date": "2040-01-10", "amount": "680.12", "status": "paid",
                     "paid_date": "2040-02-05"}]}
    fee = lambda no, d, m: {"invoice_number": no, "invoice_date": d, "contract_id": "C-LF", "currency": "USD",
                            "total": "12.65", "lines": [{"description": "Late fee", "amount": "12.65", "late_fee": {
                                "on_invoice": "T-1", "overdue_amount": "680.12", "month_number": m}}]}
    with tempfile.TemporaryDirectory() as tmp:
        write_state(Path(tmp), [("contract", contract, "2039-12-01"), ("contract_instrument", amd, "2040-01-20"),
                                ("email", notice, "2040-02-10", "rita@car.example"), ("invoice", inv, "2040-03-21"),
                                ("tracking_export", track, "2040-03-31"), ("contract", lf, "2039-01-01"),
                                ("ap_register", reg, "2040-02-28"), ("invoice", fee("F-1", "2040-02-20", 1), "2040-02-21"),
                                ("invoice", fee("F-2", "2040-03-20", 2), "2040-03-21")])
        r1 = harness.report("2040-04-30", "supervised", tmp)
        r2 = harness.report("2040-05-31", "supervised", tmp)
    got = {i["invoice_id"]: i for i in r1["findings"]}
    c = got["IN-1"]                         # billed 17600; amendment 15400, notice 16500 INR, 80 INR per USD
    assert (c["verdict"], c["amount_range"], c["action"]) == ("conflict", ["13.75", "27.50"], "escalate"), c
    assert (got["F-2"]["class"], got["F-2"]["amount_usd"]) == ("late_fee_not_owed", "12.65"), got.get("F-2")
    assert "F-1" not in got, got["F-1"]     # 26 days late, 5 days' grace: month 1 is owed
    again = {i["invoice_id"]: i for i in r2["findings"]}
    assert [(again[k]["id"], again[k]["supersedes"]) for k in ("IN-1", "F-2")] == \
        [(got[k]["id"], []) for k in ("IN-1", "F-2")], (again["IN-1"], again["F-2"])


def margin_check() -> None:
    """Margin step-down, from hand-written facts: spend crosses 10,000 on the 2nd invoice; the 3rd and 4th still bill
    12% instead of 8%: (240 - 160) + (120 - 80) = 120.00, one cashback item for the quarter, no overcharges."""
    contract = {"contract_id": "M-1", "carrier_party": "CAR", "currency": "USD", "effective_date": "2040-01-01",
                "end_date": "2040-12-31", "charge_codes": [{"code": "COST", "name": "Carrier cost", "aliases": []},
                                                           {"code": "MARGIN", "name": "Handling margin", "aliases": []}],
                "clauses": {"qualifying_spend": {"include_codes": ["COST"], "include_annex_d": False},
                            "handling_margin": {"applies": True, "percent": "12", "step_down_percent": "8",
                                                "step_down_after_spend": "10000.00"}}}
    docs = [("contract", contract, "2039-12-01")]
    for n, (d, cost) in enumerate([("2040-01-10", 6000), ("2040-02-10", 5000), ("2040-03-10", 2000),
                                   ("2040-03-20", 1000)], 1):
        docs.append(("invoice", {"invoice_number": f"M-{n}", "invoice_date": d, "contract_id": "M-1",
                                 "currency": "USD", "total": str(cost * 1.12), "lines": [
                                     {"awb": f"A{n}", "description": "Carrier cost", "amount": f"{cost}.00"},
                                     {"awb": f"A{n}", "description": "Handling margin", "rate": "12",
                                      "rate_is_percent": True, "percent_base": f"{cost}.00",
                                      "amount": f"{cost * 12 / 100:.2f}"}]}, d))
    with tempfile.TemporaryDirectory() as tmp:
        write_state(Path(tmp), docs)
        early = engine.build(Path(tmp), date(2040, 3, 31), "supervised", [], {})
        assert not early["portfolio"]["cashback"], "reported before the quarter closed"
        rep = engine.build(Path(tmp), date(2040, 4, 30), "supervised", [], {})
    cb = rep["portfolio"]["cashback"]
    assert [(i["period"], i["credit_type"], i["amount_usd"], i["action"]) for i in cb] == \
        [("2040-Q1", "margin_step_down", "120.00", "draft:claim_credit")], cb
    assert not rep["findings"], rep["findings"]


def main() -> int:
    margin_check()
    review_check()
    print("ok: INR conflict range in USD, late-fee month 2 caught, unchanged keys keep their id")
    with tempfile.TemporaryDirectory() as tmp:
        build_state(Path(tmp))
        for mode, act in (("supervised", "draft:dispute"), ("autonomous", "execute:dispute")):
            rep = engine.build(Path(tmp), date(2032, 8, 31), mode, [], {})
            got = {i["invoice_id"]: i for i in rep["findings"]}
            it = got["TX-US-2032-5394"]
            assert (it["verdict"], it["amount_usd"], it["action"]) == ("finding", "196.92", act), it
            assert "TX-US-2032-5397" not in got, got["TX-US-2032-5397"]
            mb = Mailbox(BOX)
            bad = [e for e in it["evidence"] if mb.check(e["doc"], e["page"], e["quote"])]
            assert not bad and len(it["evidence"]) >= 3, (bad, it["evidence"])
    bad = json.loads(json.dumps(FACTS["batch_01/0003.eml#TX-US-2032-5394.pdf"][1]))
    assert engine.arith_problems(bad) == []
    bad["lines"][0]["amount"] = "1621.29"                      # one misread digit
    assert any("6.69" in p or "add to" in p for p in engine.arith_problems(bad)), engine.arith_problems(bad)
    print("ok: margin step-down -> one 120.00 cashback item for the quarter")
    print("ok: overcharge example -> 196.92 (draft/execute), decoy left alone, every quote on its page")
    return 0


if __name__ == "__main__":
    sys.exit(main())
