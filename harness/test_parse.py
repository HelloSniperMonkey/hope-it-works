"""Number and date parsing in engine.py: the formats real carriers print.   python harness/test_parse.py"""
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import D, Contract, code_from, day, split_awb  # noqa: E402

for raw, want in [("1,701.31", "1701.31"), ("1.701,31", "1701.31"), ("1 701,31", "1701.31"), ("1,234", "1234"),
                  ("12,5", "12.5"), ("1.234.567", "1234567"), ("USD 45.00", "45.00"), ("(12.50)", "-12.50"),
                  ("-12.50", "-12.50"), ("18.38%", "18.38"), ("\u20ac 99,90", "99.90"), ("106 kg", "106"), ("", None),
                  (None, None), ("n/a", None)]:
    assert D(raw) == (Decimal(want) if want is not None else None), (raw, D(raw))
for raw, want in [("2036-02-04", date(2036, 2, 4)), ("04.02.2036", date(2036, 2, 4)), ("25/12/2030", date(2030, 12, 25)),
                  ("12/25/2030", date(2030, 12, 25)), ("03/06/2030", None), ("4 Feb 2036", date(2036, 2, 4)),
                  ("February 4, 2036", date(2036, 2, 4)), ("31.02.2036", None), (None, None)]:
    assert day(raw) == want, (raw, day(raw))
codes = {"screening": "SEC", "freight": "FRT", "fuel surcharge": "FUEL", "fuel": "FUEL"}
for label, want in [("Airport screening", "SEC"), ("Net billed freight", "FRT"), ("Fuel surcharge on freight", "FUEL"),
                    ("Handling", None)]:
    assert code_from(codes, label) == want, (label, code_from(codes, label))
for desc, want in [("AF-100 Transportation", ("AF-100", "Transportation")), ("AWB AF-100 / Screening", ("AF-100", "Screening")),
                   ("XX-9 Fuel", (None, "XX-9 Fuel"))]:
    ln = {"description": desc, "awb": None}
    split_awb(ln, {"AF100"})
    assert (ln["awb"], ln["description"]) == want, (desc, ln)
one = Contract({"rates": [{"lane": "N1-S2"}]}, "c", date(2041, 1, 1))
two = Contract({"rates": [{"lane": "N1-S2"}, {"lane": "S2-N1"}]}, "c", date(2041, 1, 1))
assert (one.lane_of(None), one.lane_of("s2-n1"), two.lane_of(None)) == ("N1-S2", "S2-N1", "")
print("ok: numbers and dates parse in every common carrier format; an ambiguous 03/06/2030 stays unread; labels, waybills in labels and "
      "single-lane contracts resolve")
