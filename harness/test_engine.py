"""One runnable check for the rules, no API needed: the facts the extractor should return for
data/examples/overcharge, run through the engine, must give the worked example's answer, with quotes the grader can
find.   python harness/test_engine.py
"""
from __future__ import annotations

import json
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
            rec["docs"].append({"ref": d.ref, "kind": kind, "scan": False,
                                "facts": harness._resolve(json.loads(json.dumps(facts)), d.pages)})
        (state / "docs" / (group[0].ref.replace("/", "_") + ".json")).write_text(json.dumps(rec))


def main() -> int:
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
    print("ok: overcharge example -> 196.92 (draft/execute), decoy left alone, every quote on its page")
    return 0


if __name__ == "__main__":
    sys.exit(main())
