"""Number and date parsing in engine.py: the formats real carriers print.   python harness/test_parse.py"""
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import D, day  # noqa: E402

for raw, want in [("1,701.31", "1701.31"), ("1.701,31", "1701.31"), ("1 701,31", "1701.31"), ("1,234", "1234"),
                  ("12,5", "12.5"), ("1.234.567", "1234567"), ("USD 45.00", "45.00"), ("(12.50)", "-12.50"),
                  ("-12.50", "-12.50"), ("18.38%", "18.38"), ("\u20ac 99,90", "99.90"), ("106 kg", "106"), ("", None),
                  (None, None), ("n/a", None)]:
    assert D(raw) == (Decimal(want) if want is not None else None), (raw, D(raw))
for raw, want in [("2036-02-04", date(2036, 2, 4)), ("04.02.2036", date(2036, 2, 4)), ("25/12/2030", date(2030, 12, 25)),
                  ("12/25/2030", date(2030, 12, 25)), ("03/06/2030", None), ("4 Feb 2036", date(2036, 2, 4)),
                  ("February 4, 2036", date(2036, 2, 4)), ("31.02.2036", None), (None, None)]:
    assert day(raw) == want, (raw, day(raw))
print("ok: numbers and dates parse in every common carrier format; an ambiguous 03/06/2030 stays unread")
