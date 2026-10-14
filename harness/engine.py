"""The judging half: plain Python over the extracted facts. Every rule here is a rule from GLOSSARY.md.

report() rebuilds the whole picture from the facts received by as_of, so a new document or a feedback item changes
the answer the same way a re-read would. Money is Decimal, rounded half up to cents only where the glossary says.
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

CENT = Decimal("0.01")
REVIEW_AT = Decimal("5000.00")       # outbound remedies at or above this go to a person, in either mode
ZERO = Decimal("0")
FREIGHT, FUEL, SECURITY, LATE = "FRT", "FUEL", "SEC", "LATE"
NOT_PRICED = {"LATE", "TAX", "DUTY", "COST", "MARGIN", "HDL", "GST"}
INVOICE_CLASSES = ("overcharge", "duplicate", "waived_but_billed", "late_fee_not_owed")


# ---- small helpers ----------------------------------------------------------------------------------------------------
def rhu(x) -> Decimal:
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def D(x) -> Decimal | None:
    """A printed number -> Decimal, whichever way it is written: 1,234.56 / 1.234,56 / 1 234,56 / (12.50) / -12.50.
    The last separator is the decimal point when both appear; a lone comma is a decimal point unless it groups
    exactly three digits."""
    if x is None or isinstance(x, bool):
        return None
    s = str(x).strip().replace("\u2212", "-").replace("\u00a0", " ")
    credit = bool(re.search(r"(?<=\d)\s*CR\s*$", s, re.I))
    s = re.sub(r"(?<=\d)\s*(?:CR|DR)\s*$", "", s, flags=re.I)
    s = re.sub(r"[\s%$\u20ac\u00a3\u20b9]|USD|INR|EUR|GBP|CAD|AUD|JPY|kg|lbs?|CWT", "", s, flags=re.I)
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-") or s.endswith("-") or credit
    s = s.strip("()-+")
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in s:
        s = s.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", s) else s.replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        value = -Decimal(s) if neg and s else (Decimal(s) if s else None)
        return value if value is not None and value.is_finite() else None
    except InvalidOperation:
        return None


def day(x) -> date | None:
    """ISO, DD.MM.YYYY, '4 Feb 2036', or a slash date whose order is unambiguous (a part above 12); else None."""
    from datetime import datetime
    t = str(x or "").strip()
    try:
        return date.fromisoformat(t[:10])
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})([./])(\d{1,2})\2(\d{4})", t)
    if m:
        a, sep, b, y = int(m[1]), m[2], int(m[3]), int(m[4])
        d, mo = (a, b) if sep == "." or a > 12 else ((b, a) if b > 12 else (None, None))
        try:
            return date(y, mo, d) if d else None
        except ValueError:
            return None
    for fmt in ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    return None


def money(x) -> str:
    return f"{rhu(x):.2f}"


def norm_inv(s) -> str:
    return re.sub(r"[\s#:]", "", str(s or "")).upper()


def norm_awb(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def norm_lane(s) -> str:
    return re.sub(r"\s", "", str(s or "")).upper()


def norm_label(s) -> str:
    return " ".join(re.sub(r"[^a-z0-9%+.\-/ ]", " ", str(s or "").lower()).split()).strip(" .")


def yes(x) -> bool:
    return str(x).strip().lower() in ("yes", "true", "1")


def month_of(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def quarter_of(d: date) -> str:
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def quarter_bounds(q: str) -> tuple[date, date]:
    y, n = int(q[:4]), int(q[-1])
    first = date(y, 3 * n - 2, 1)
    nxt = date(y + (n == 4), (3 * n) % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:                              # 29 Feb
        return d.replace(year=d.year + n, day=28)


def cite(ref: str, obj, key: str = "src") -> dict | None:
    """The evidence entry for the line(s) a fact was read from (resolved to page text at ingest)."""
    q = obj.get(key + "_q") if isinstance(obj, dict) else None
    if not q:
        return None
    return {"doc": ref, "page": q["page"], "quote": q["quote"]}


def cites(*xs) -> list[dict]:
    out, seen = [], set()
    for x in xs:
        for c in (x if isinstance(x, list) else [x]):
            if c and len(re.sub(r"\s", "", c["quote"])) >= 12 and (c["doc"], c["page"], c["quote"]) not in seen:
                seen.add((c["doc"], c["page"], c["quote"]))
                out.append(c)
    return out


def band_bounds(row) -> tuple[Decimal | None, Decimal | None]:
    lo, hi = D(row.get("lo")), D(row.get("hi"))
    if lo is None and hi is None:
        m = re.match(r"\s*([\d.]+)\s*(?:-\s*([\d.]+)|\+)", str(row.get("band") or ""))
        if m:
            lo, hi = D(m.group(1)), D(m.group(2)) if m.group(2) else None
    return lo, hi


def is_min_row(row) -> bool:
    return bool(row.get("is_minimum_charge")) or "minimum" in str(row.get("band") or "").lower()


def band_of(rows: list[dict], cw: Decimal) -> dict | None:
    for r in rows:
        if is_min_row(r):
            continue
        lo, hi = band_bounds(r)
        if lo is not None and lo <= cw and (hi is None or cw <= hi):
            return r
    return None


def min_of(rows: list[dict]) -> dict | None:
    return next((r for r in rows if is_min_row(r)), None)


# ---- the facts, as of a date ------------------------------------------------------------------------------------------
class Contract:
    def __init__(self, f: dict, ref: str, received: date):
        self.f, self.ref, self.received = f, ref, received
        self.id = str(f.get("contract_id") or "").strip()
        self.carrier, self.shipper = f.get("carrier_party"), f.get("shipper_party")
        self.currency = str(f.get("currency") or "USD").upper()[:3]
        self.effective, self.end = day(f.get("effective_date")), day(f.get("end_date"))
        self.periods = [(p["name"], day(p["first_pickup"]), day(p["last_pickup"])) for p in f.get("rate_periods") or []]
        self.rates: dict[str, list] = defaultdict(list)
        for r in f.get("rates") or []:
            self.rates[norm_lane(r["lane"])].append(r)
        self.fuel = {r["month"]: r for r in f.get("fuel") or []}
        self.acc = {str(r["code"]).upper(): r for r in f.get("accessorials") or []}
        self.codes: dict[str, str] = {}
        for c in f.get("charge_codes") or []:
            code = str(c["code"]).upper()
            for lab in [c.get("name"), code] + list(c.get("aliases") or []):
                if lab:
                    self.codes.setdefault(norm_label(lab), code)
        for code, a in self.acc.items():
            self.codes.setdefault(norm_label(a.get("name")), code)
        self.fx = {r["month"]: r for r in f.get("fx") or []}
        self.persons = f.get("authorised_persons") or []
        self.cl = f.get("clauses") or {}

    def clause(self, name: str) -> dict:
        return self.cl.get(name) or {}

    def lane_of(self, raw) -> str:
        """A line's lane; an invoice that prints none is on the contract's only lane, if it has just one."""
        return norm_lane(raw) or (next(iter(self.rates)) if len(self.rates) == 1 else "")

    def code_of(self, label: str) -> str | None:
        return code_from(self.codes, label)

    def code_loose(self, label: str) -> str | None:
        """Like code_of, for a charge a person named in passing ("the account fee"): the one code whose label shares
        a distinctive word with it. Several equally good codes -> None, we don't guess."""
        exact = self.code_of(label)
        if exact:
            return exact
        generic = {"fee", "fees", "charge", "charges", "surcharge", "the", "a", "of", "on", "for", "monthly", "and"}
        want = set(norm_label(label).split()) - generic
        score: dict[str, int] = defaultdict(int)
        for alias, code in self.codes.items():
            score[code] = max(score[code], len(want & (set(alias.split()) - generic)))
        top = sorted(score.items(), key=lambda kv: -kv[1])
        return top[0][0] if top and top[0][1] > 0 and (len(top) == 1 or top[1][1] < top[0][1]) else None

    def period_of(self, pickup: date) -> str | None:
        for name, a, b in self.periods:
            if a and b and a <= pickup <= b:
                return name
        if self.periods and pickup and self.periods[-1][2] and pickup > self.periods[-1][2] \
                and self.f.get("last_period_continues"):
            return self.periods[-1][0]
        return None if self.periods else ""

    def person(self, category: str, party: str | None = None, title: str | None = None) -> dict | None:
        for p in self.persons:
            if category in (p.get("categories") or []) and (party is None or p.get("party") == party) \
                    and (title is None or title.lower() in str(p.get("title") or "").lower()):
                return p
        return None

    def holder(self, category: str) -> dict:
        p = self.person(category, self.carrier) or self.person(category)
        if not p:
            return {"party": self.carrier} if self.carrier else {}
        return {"party": p.get("party") or self.carrier, "person": p.get("name"), "role": p.get("title")}

    def kam(self) -> dict:
        p = self.person("notices", self.carrier, "key account") or self.person("notices", self.carrier)
        return {"party": self.carrier, **({"person": p.get("name"), "role": p.get("title")} if p else {})}


def code_from(codes: dict[str, str], label: str) -> str | None:
    lab = norm_label(label)
    if lab in codes:
        return codes[lab]
    for fits in (lambda a: lab.startswith(a + " "), lambda a: lab.endswith(" " + a)):    # "Airport screening"
        best = max((a for a in codes if a and fits(a)), key=len, default=None)
        if best:
            return codes[best]
    return None


def split_awb(ln: dict, known: set) -> None:
    """A line that printed its waybill inside the label ("AF-100 Transportation"): take it out, if the mailbox knows
    that waybill from tracking or an air waybill. An unknown leading token is left as part of the label."""
    if ln.get("awb"):
        return
    m = re.match(r"\s*(?:(?:h|m)?awb|waybill|consignment|shipment)?\s*(?:no\.?|#)?\s*(\S+?)\s*[/:]?\s+(\S.*)",
                 str(ln.get("description") or ""), re.I)
    if m and norm_awb(m.group(1)) in known:
        ln["awb"], ln["description"] = m.group(1), m.group(2)


class World:
    """Everything the mailbox has said by as_of (end of day UTC), indexed for the rules."""

    def __init__(self, state: Path, as_of: date, feedback: list[dict]):
        self.as_of = as_of
        self.feedback = sorted((fb for fb in feedback if not day(fb.get("_decided_at")) or
                                day(fb["_decided_at"]) <= as_of), key=lambda fb: fb.get("_decided_at") or "")
        self.contracts: dict[str, Contract] = {}
        self.instruments, self.notices, self.waivers, self.extensions = [], [], [], []
        self.events: dict[str, list] = defaultdict(list)      # awb -> [(event, date, source, row/ev, ref)]
        self.invoices: dict[str, list] = defaultdict(list)    # invoice id -> copies, in order received
        self.credit_notes, self.registers, self.refs, self.email_fx = [], [], [], []
        self.credit_ids = set()
        self.awbs: dict[str, tuple] = {}
        self.dgds: dict[str, tuple] = {}
        self.automation: list[dict] = []
        self.docs_received: dict[str, date] = {}
        self.unreadable = []
        self.email_docs: dict[str, list] = defaultdict(list)       # email ref -> [{"pages": ...}]
        recs = []
        for f in sorted((state / "docs").glob("*.json")):
            recs.append(json.loads(f.read_text()))
        recs.sort(key=lambda r: (r["meta"]["received"], r["meta"]["email"]))
        for r in recs:
            meta = r["meta"]
            rec_day = day(meta["received"])
            if rec_day is None or rec_day > as_of:
                continue
            email_facts = next((d["facts"] for d in r["docs"] if d["kind"] == "email"), {}) or {}
            self.email_docs[meta["email"]].append({"pages": next((d.get("pages") for d in r["docs"]
                                                                 if d["kind"] == "email"), None)})
            for d in r["docs"]:
                self.docs_received[d["ref"]] = rec_day
                if d["kind"] == "unreadable" or d.get("facts") is None:
                    self.unreadable.append(d["ref"])
                self._add(d["kind"], d.get("facts") or {}, d["ref"], meta, rec_day, email_facts, d.get("pages"))
        known = set(self.events) | set(self.awbs)
        for ln in [ln for cps in self.invoices.values() for cp in cps for ln in cp["f"].get("lines") or []] + \
                  [ln for cn in self.credit_notes for ln in cn.get("lines") or []]:
            split_awb(ln, known)

    def _add(self, kind, f, ref, meta, rec, email_facts, pages=None):
        ctx = {"ref": ref, "meta": meta, "received": rec, "pages": pages}
        for a in f.get("automation_directed") or []:
            if cite(ref, a):
                self.automation.append(cite(ref, a))
        for x in f.get("referenced_documents") or []:
            self.refs.append({**x, **ctx, "kind": kind, "facts": f})
        if kind == "email":
            for n in f.get("rate_notices") or []:
                self.notices.append({**n, **ctx})
            for w in f.get("waivers") or []:
                self.waivers.append({**w, **ctx})
            for x in f.get("due_date_extensions") or []:
                self.extensions.append({**x, **ctx})
            for ev in f.get("shipment_events") or []:
                if ev.get("awb") and day(ev.get("date")) and not self.aimed_at_machines(ref, ev):
                    self.events[norm_awb(ev["awb"])].append(
                        {"event": ev["event"], "date": day(ev["date"]), "source": "email", "fault": ev.get("fault"),
                         "reason": ev.get("reason_code"), "obj": ev, "ref": ref, "meta": meta})
            for x in f.get("fx_rates") or []:
                if not self.aimed_at_machines(ref, x):
                    self.email_fx.append({**x, **ctx})
        elif kind == "contract" and f.get("contract_id"):
            c = Contract(f, ref, rec)
            self.contracts.setdefault(c.id, c)
        elif kind == "contract_instrument":
            self.instruments.append({**f, **ctx})
        elif kind == "invoice" and f.get("invoice_number"):
            self.invoices[norm_inv(f["invoice_number"])].append({**ctx, "f": f, "email": email_facts})
        elif kind == "credit_note":
            key = (str(f.get("contract_id") or "").upper(), norm_inv(f.get("number")))
            if not key[1] or key not in self.credit_ids:
                self.credit_notes.append({**f, **ctx})
                self.credit_ids.add(key)
        elif kind == "air_waybill" and f.get("awb"):
            self.awbs.setdefault(norm_awb(f["awb"]), (f, ref))
        elif kind == "dg_declaration" and f.get("awb"):
            self.dgds.setdefault(norm_awb(f["awb"]), (f, ref))
        elif kind == "tracking_export":
            for row in f.get("rows") or []:
                if row.get("awb") and day(row.get("date")):
                    self.events[norm_awb(row["awb"])].append(
                        {"event": row["event"], "date": day(row["date"]), "source": "tracking", "fault": None,
                         "reason": row.get("reason_code"), "obj": row, "ref": ref, "meta": meta})
        elif kind == "ap_register":
            self.registers.append({**f, **ctx})

    # -- people and authority ------------------------------------------------------------------------------------------
    def authorised(self, c: Contract | None, category: str, email: str | None, name: str | None, on: date | None,
                   party: str | None = None, invs=(), lane=None, cls=None):
        """-> (ok, schedule-1 evidence or None, feedback item or None). Schedule 1 is matched on the address it lists
        (notices count only from those addresses); a confirmed feedback fact covers exactly its person, contract and
        category, inside its dates."""
        if c is None:
            return False, None, None
        em = (email or "").strip().lower()
        for p in c.persons:
            if category not in (p.get("categories") or []) or (party and p.get("party") != party):
                continue
            if em and em == str(p.get("email") or "").strip().lower():
                return True, cite(c.ref, p), None
            if not em and name and norm_label(name) == norm_label(p.get("name")):
                return True, cite(c.ref, p), None
        for fb in reversed(self.feedback):
            fact = fb.get("fact") or {}
            if fb.get("decision") != "confirm" or fact.get("type") != "sender_authority":
                continue
            cats = (fact.get("value") or {}).get("categories") or []
            subj = fact.get("subject") or {}
            if category not in cats or norm_label(subj.get("person")) != norm_label(name) or \
                    (subj.get("party") and subj["party"] != party):
                continue
            ids = {norm_inv(x) for x in invs if x}
            if feedback_matches(fb, on, contract_id=c.id, lane=lane, person=name, category=category,
                                party=party, invoice_id=next(iter(ids)) if len(ids) == 1 else None,
                                **{"class": cls or {"rates": "overcharge", "waivers": "waived_but_billed"}.get(category)}):
                return True, None, fb
        return False, None, None

    def aimed_at_machines(self, ref: str, obj: dict) -> bool:
        """Text addressed to automated readers is data: a term written in it binds nobody."""
        q = cite(ref, obj)
        return bool(q) and any(a["doc"] == q["doc"] and (q["quote"] in a["quote"] or a["quote"] in q["quote"])
                               for a in self.automation)

    # -- documents -----------------------------------------------------------------------------------------------------
    def contract_for(self, contract_id: str | None) -> Contract | None:
        if not contract_id:
            return None
        cid = str(contract_id).strip()
        return self.contracts.get(cid) or next((c for k, c in self.contracts.items() if k.upper() == cid.upper()),
                                               None)

    def register_row(self, inv_id: str, earliest_if=None):
        """The latest AP register's row for an invoice (later registers replace earlier ones); with `earliest_if`, the
        earliest register whose row satisfies it (the first ledger that proves something)."""
        best = None
        for reg in self.registers:
            for row in reg.get("rows") or []:
                if norm_inv(row.get("invoice_number")) == inv_id:
                    if earliest_if and not earliest_if(row):
                        continue
                    k = (day(reg.get("as_of")) or reg["received"], reg["received"])
                    if best is None or (k < best[0] if earliest_if else k >= best[0]):
                        best = (k, row, reg["ref"])
        return (best[1], best[2]) if best else (None, None)

    def due_date(self, inv_id: str, c: Contract | None, base_due: date | None, invs=(), cls=None):
        """Due date after any extension from the carrier's payment-terms person -> (date, evidence, feedback).
        `invs`: the invoices whose keys this decides, for one-off feedback."""
        due, ev, fb_used = base_due, [], None
        for x in self.extensions:
            if norm_inv(x.get("invoice_number")) != inv_id or yes(x.get("quoted_only")) or \
                    self.aimed_at_machines(x["ref"], x):
                continue
            email = x["meta"]["from_addr"] if not x.get("written_by_email") else x["written_by_email"]
            ok, sch, fb = self.authorised(c, "payment_terms", email, x.get("written_by_name") or
                                          x["meta"]["from_name"], x["received"], c.carrier if c else None, invs, cls=cls)
            nd = day(x.get("new_due_date"))
            if ok and nd:
                due, ev, fb_used = nd, cites(cite(x["ref"], x), sch), fb
        return due, ev, fb_used

    def pickup(self, awb: str | None, line: dict | None, inv_ref: str | None, c: Contract | None):
        """-> (date, source, evidence): tracking export, then a carrier "picked up" email, then the AWB, then the
        date printed on the invoice line."""
        if awb:
            evs = self.events.get(norm_awb(awb), [])
            for src in ("tracking", "email"):
                hits = [e for e in evs if e["event"] == "PICKED_UP" and e["source"] == src
                        and (src == "tracking" or self.from_carrier(e["meta"], c))]
                if hits:
                    e = hits[0]
                    return e["date"], src, cites(cite(e["ref"], e["obj"]))
            if norm_awb(awb) in self.awbs:
                f, ref = self.awbs[norm_awb(awb)]
                if day(f.get("executed_on")):
                    return day(f["executed_on"]), "awb", cites(cite(ref, f, "date_src"), cite(ref, f))
        if line and day(line.get("date")):
            return day(line["date"]), "invoice", cites(cite(inv_ref, line)) if inv_ref else []
        return None, None, []

    def from_carrier(self, meta: dict, c: Contract | None) -> bool:
        dom = meta.get("from_addr", "").split("@")[-1]
        if c is None:
            return True
        return any(str(p.get("email", "")).lower().endswith("@" + dom) and p.get("party") == c.carrier
                   for p in c.persons)

    def instruments_for(self, c: Contract) -> list[dict]:
        out = [i for i in self.instruments if str(i.get("contract_id") or "").upper() == c.id.upper()]
        return sorted(out, key=lambda i: (day(i.get("signed_on")) or i["received"],
                                         day(i.get("effective_from")) or date.min, i["received"]))

    def held_instrument(self, c: Contract, name: str) -> dict | None:
        want = norm_label(name)
        for i in self.instruments_for(c):
            if norm_label(i.get("name")) == want or want in norm_label(i.get("name")) \
                    or want in norm_label(i.get("reference")):
                return i
        return None


def in_scope(fb: dict, on: date | None) -> bool:
    sc = fb.get("scope") or {}
    a, b = day(sc.get("valid_from")), day(sc.get("valid_to"))
    return (a is None and b is None) if on is None else ((a is None or a <= on) and (b is None or on <= b))


def feedback_matches(fb: dict, on: date | None, **context) -> bool:
    """Every scope filter must be proved. A one-off is also restricted to the full finding key.

    Standing dates describe the covered documents/shipments. One-off approvals can retrospectively approve an old
    charge; their availability is governed by decided_at, and their named key/months still restrict coverage.
    """
    scope = fb.get("scope") or {}
    filters = dict(scope.get("applies_to") or {})
    if scope.get("kind") == "one_off":
        for k, v in (fb.get("finding_key") or {}).items():
            if k in filters and scope_value(k, filters[k]) != scope_value(k, v):
                return False
            filters[k] = v
    for k, v in filters.items():
        if v is not None and (context.get(k) is None or scope_value(k, v) != scope_value(k, context[k])):
            return False
    return scope.get("kind") == "one_off" or in_scope(fb, on)


def scope_value(key: str, value):
    if key == "invoice_id":
        return norm_inv(value)
    if key == "lane":
        return norm_lane(value)
    if key == "person":
        return norm_label(value)
    return str(value).strip()


def later_than_feedback(src: dict, fb: dict) -> bool:
    decided = day(fb.get("_decided_at"))
    if decided:
        return (day((src.get("obj") or {}).get("signed_on")) or src["received"]) > decided
    valid = day((fb.get("scope") or {}).get("valid_from"))
    return bool(src.get("eff") and valid and src["eff"] > valid)


# ---- report items -----------------------------------------------------------------------------------------------------
def item(cls: str, key: dict, verdict: str, *, amount=None, rng=None, deadline=None, evidence=(), missing=None,
         **internal) -> dict:
    it = {"id": "", "class": cls, **key, "verdict": verdict,
          "amount_usd": money(amount) if amount is not None and verdict == "finding" else None,
          "amount_range": [money(rng[0]), money(rng[1])] if rng else None,
          "deadline": deadline.isoformat() if isinstance(deadline, date) else deadline,
          "action": "none", "confidence": 0.5, "evidence": cites(*evidence), "missing_doc": missing,
          "supersedes": [], "learned_at": None}
    it.update({"_" + k: v for k, v in internal.items()})
    return it


def key_of(it: dict) -> str:
    if it["class"] == "cashback":
        return f"cashback:{it['contract_id']}/{it['period']}/{it['credit_type']}"
    if it["class"] == "expiry":
        return f"expiry:{it['contract_id']}"
    return f"{it['class']}:{it['invoice_id']}"


def learned(fb: dict | None) -> dict | None:
    return {"feedback_item_id": fb["item_id"], "batch": fb.get("_batch")} if fb else None


# ---- pricing ----------------------------------------------------------------------------------------------------------
class Pricer:
    """Reprices invoice lines from the terms in force on the pickup date."""

    def __init__(self, w: World, c: Contract, inv: str | None = None):
        self.w, self.c, self.inv = w, c, inv

    def fx(self, inv_date: date):
        m = month_of(inv_date)
        if m in self.c.fx:
            return D(self.c.fx[m]["inr_per_usd"]), cites(cite(self.c.ref, self.c.fx[m]))
        for i in self.w.instruments_for(self.c):
            for r in i.get("fx") or []:
                if r.get("month") == m:
                    return D(r["inr_per_usd"]), cites(cite(i["ref"], r))
        for r in self.w.email_fx:
            if r.get("month") == m and str(r.get("contract_id") or self.c.id).upper() == self.c.id.upper():
                ok, sch, _ = self.w.authorised(self.c, "treasury", r["meta"]["from_addr"], r["meta"]["from_name"],
                                               r["received"], None, {self.inv})
                if ok:
                    return D(r["inr_per_usd"]), cites(cite(r["ref"], r), sch)
        return None, []

    def fx_missing(self) -> dict:
        up = self.c.f.get("fx_update") or {}
        cats = up.get("issued_by_category") or ["treasury"]
        return {"name": up.get("name") or "Annex F update", "holder": self.c.holder(cats[0]),
                "_cite": cite(self.c.ref, up)}

    def fuel_pct(self, pickup: date):
        m = month_of(pickup)
        pct, ev = None, []
        if m in self.c.fuel:
            pct, ev = D(self.c.fuel[m]["percent"]), cites(cite(self.c.ref, self.c.fuel[m]))
        for i in self.w.instruments_for(self.c):
            if not i.get("signed_by_both"):
                continue
            for r in i.get("fuel_changes") or []:
                if r.get("month") == m:
                    pct, ev = D(r["percent"]), cites(cite(i["ref"], r))
        return pct, ev

    def accessorial(self, code: str, pickup: date | None):
        a = self.c.acc.get(code)
        fee, ev = (D(a["fee"]), cites(cite(self.c.ref, a))) if a else (None, [])
        for i in self.w.instruments_for(self.c):
            eff = day(i.get("effective_from"))
            if not i.get("signed_by_both") or (eff and pickup and eff > pickup):
                continue
            for r in i.get("accessorial_changes") or []:
                if str(r.get("code") or "").upper() == code or self.c.code_of(r.get("name")) == code:
                    fee, ev = D(r["fee"]), cites(cite(i["ref"], r))
        return fee, (a or {}), ev

    # -- the rate for a lane on a pickup date: the term resolver -------------------------------------------------------
    def freight_sources(self, lane: str, pickup: date):
        """-> {"kind": "ok"|"conflict"|"missing"|"unknown", ...}. All of "which document wins" lives here."""
        c, w = self.c, self.w
        period = c.period_of(pickup)
        msa_rows = [{**r, "rate": next((pr["rate"] for pr in r.get("period_rates") or []
                                        if not period or pr["period"] == period),
                                       (r.get("period_rates") or [{}])[0].get("rate"))}
                    for r in c.rates.get(lane, [])]
        msa = {"label": "MSA Annex A", "rows": msa_rows, "ref": c.ref, "level": 1, "eff": c.effective,
               "kind": "msa", "received": c.received} if msa_rows else None
        if c.effective and pickup < c.effective:
            return {"kind": "unknown"}
        # signed amendments and the annexes they bring in, latest effective date first
        amd, missing = None, None
        for i in self.w.instruments_for(c):
            eff, to = day(i.get("effective_from")), day(i.get("effective_to"))
            if not i.get("signed_by_both") or (eff and eff > pickup) or (to and pickup > to):
                continue
            rows = [r for r in i.get("rate_changes") or [] if norm_lane(r.get("lane")) == lane
                    and (not r.get("period") or not period or r["period"] == period)]
            if rows:
                amd, missing = {"label": i.get("reference") or i.get("name"), "rows": rows, "ref": i["ref"],
                                "level": 1, "eff": eff, "kind": "amendment", "received": i["received"],
                                "obj": i}, None
            for x in i.get("referenced_documents") or []:
                frm, upto = day(x.get("applies_from")), day(x.get("applies_to"))
                if x.get("governs") != "rates" or lane not in [norm_lane(l) for l in x.get("lanes") or []] \
                        or (frm and frm > pickup) or (upto and pickup > upto):
                    continue
                held = None if x.get("included_here") else self.w.held_instrument(c, x["name"])
                own = any(norm_lane(r.get("lane")) == lane for r in i.get("rate_changes") or [])
                in_contract = norm_label(x.get("name")) in {norm_label(y.get("name")) for y in
                                                            c.f.get("referenced_documents") or []
                                                            if y.get("included_here")}
                if x.get("included_here") or own or in_contract:
                    continue            # the rates are on this page, or the name is a part of the held contract
                if held:
                    hrows = [r for r in held.get("rate_changes") or [] if norm_lane(r.get("lane")) == lane]
                    amd, missing = {"label": x["name"], "rows": hrows, "ref": held["ref"], "level": 1,
                                    "eff": frm or day(held.get("effective_from")), "kind": "annex",
                                    "received": held["received"], "obj": held}, None
                else:
                    missing = {"name": x["name"], "holder": c.holder("rates"), "_cite": cite(i["ref"], x),
                               "eff": frm}
        # rate notices: written notice from the carrier's rates person (clause 3.5), or one a person accepted
        notice, notice_fb = None, None
        for n in w.notices:
            if norm_lane(n.get("lane")) != lane or yes(n.get("quoted_only")):
                continue
            if n.get("contract_id") and str(n["contract_id"]).upper() != c.id.upper():
                continue
            a, b = day(n.get("effective_from")), day(n.get("effective_to"))
            if (a and pickup < a) or (b and pickup > b):
                continue
            accepted = self.accepted(lane, n, pickup)
            email = n.get("written_by_email") or n["meta"]["from_addr"]
            ok, sch, fb = w.authorised(c, "rates", email, n.get("written_by_name") or n["meta"]["from_name"],
                                       n["received"], c.carrier, {self.inv}, lane=lane)
            if accepted and amd and later_than_feedback(amd, accepted) and not ok:
                accepted = None   # an older confirmed notice cannot overrule a later signed amendment
            if not ok and not accepted:
                continue
            if w.aimed_at_machines(n["ref"], n):
                continue
            notice = {"label": f"rate notice {n.get('reference') or ''}".strip(), "rows": n.get("rows") or [],
                      "ref": n["ref"], "level": 2, "eff": a, "kind": "notice", "received": n["received"],
                      "obj": n, "sch": sch}
            notice_fb = accepted or fb
        if missing and (not amd or (missing.get("eff") or date.min) >= (amd["eff"] or date.min)):
            return {"kind": "missing", "missing": missing, "msa": msa}
        if amd and notice:
            prec, fb = self.precedence(lane, pickup)
            if fb:
                later = later_than_feedback(amd, fb)
                use = (prec or {}).get("use", "")
                return {"kind": "ok", "src": amd if later or "notice" not in use else notice, "msa": msa,
                        "fb": None if later else fb}
            rule = (c.clause("precedence") or {}).get("notice_vs_amendment") or "not_stated"
            if rule == "unranked":
                return {"kind": "conflict", "a": amd, "b": notice, "msa": msa, "fb": notice_fb,
                        "clause": cite(c.ref, c.clause("precedence"), "unranked_src")}
            return {"kind": "ok", "src": notice if rule == "notice_wins" else amd, "msa": msa, "fb": notice_fb}
        if amd:
            return {"kind": "ok", "src": amd, "msa": msa}
        if notice:
            return {"kind": "ok", "src": notice, "msa": msa, "fb": notice_fb}
        if msa:
            return {"kind": "ok", "src": msa, "msa": msa}
        return {"kind": "unknown"}

    def accepted(self, lane: str, n: dict, pickup: date) -> dict | None:
        """A person's close + rate_accepted: that notice is the rate for this contract and lane inside its dates."""
        for fb in reversed(self.w.feedback):
            fact = fb.get("fact") or {}
            if fact.get("type") != "rate_accepted" or fb.get("decision") not in ("close", "confirm"):
                continue
            subj, val = fact.get("subject") or {}, fact.get("value") or {}
            sc = (fb.get("scope") or {}).get("applies_to") or {}
            who = n.get("written_by_name") or n["meta"]["from_name"]
            if str(subj.get("contract_id") or sc.get("contract_id") or "").upper() != self.c.id.upper() or \
                    norm_lane(subj.get("lane") or sc.get("lane")) != lane or not feedback_matches(
                        fb, pickup, contract_id=self.c.id, lane=lane, person=who, category="rates",
                        party=self.c.carrier, invoice_id=self.inv, **{"class": "overcharge"}):
                continue
            a, b = day(val.get("effective_from")), day(val.get("effective_to"))
            if (a and pickup < a) or (b and pickup > b):
                continue
            if val.get("sent_by") and norm_label(val["sent_by"]) != norm_label(n.get("written_by_name") or
                                                                               n["meta"]["from_name"]):
                continue
            if val.get("sent_on") and day(val["sent_on"]) != n["received"]:
                continue
            return fb
        return None

    def precedence(self, lane: str, pickup: date):
        for fb in reversed(self.w.feedback):
            fact = fb.get("fact") or {}
            if fact.get("type") != "precedence" or fb.get("decision") != "resolve_conflict":
                continue
            sc = (fb.get("scope") or {}).get("applies_to") or {}
            subj = fact.get("subject") or {}
            if str(sc.get("contract_id") or subj.get("contract_id") or "").upper() == self.c.id.upper() and \
                    norm_lane(sc.get("lane") or subj.get("lane")) == lane and feedback_matches(
                        fb, pickup, contract_id=self.c.id, lane=lane, category="rates", invoice_id=self.inv,
                        **{"class": "overcharge"}):
                return fact.get("value"), fb
        return None, None

    def freight(self, src: dict, cw: Decimal):
        """-> (amount, evidence) for base freight under one source, minimum charge included."""
        row = band_of(src["rows"], cw)
        if row is None or D(row.get("rate")) is None:
            return None, []
        amt = rhu(D(row["rate"]) * cw)
        ev = [cite(src["ref"], row)]
        own = min_of(src["rows"])
        mrow = own or min_of(src.get("msa_rows") or [])
        if mrow and D(mrow.get("rate")) is not None and amt < D(mrow["rate"]):
            amt = rhu(D(mrow["rate"]))
            ev.append(cite(src["ref"] if own else src.get("msa_ref"), mrow))
        return amt, cites(*ev)


# ---- the detectors ----------------------------------------------------------------------------------------------------
def shipments(lines: list[dict]) -> dict:
    out = defaultdict(list)
    for ln in lines:
        out[norm_awb(ln.get("awb"))].append(ln)
    return out


def invoice_copies(w: World):
    """-> {invoice id: (first copy, all copies)} for invoices and statements under a contract we may not hold."""
    return {k: (v[0], v) for k, v in w.invoices.items()}


def overcharges(w: World, out: list[dict]):
    for inv_id, (copy, _) in invoice_copies(w).items():
        f, ref = copy["f"], copy["ref"]
        c = w.contract_for(f.get("contract_id"))
        if c is None:
            continue
        pr = Pricer(w, c, inv_id)
        diffs, ev, flags = [], [], {"pickup": set(), "lanes": set(), "levels": True, "issues": 0, "learned": None,
                                    "retro": False}
        conflicts: dict[str, dict] = {}      # awb -> its freight lines that two unranked sources price differently
        missing = None
        for awb, lines in shipments(f.get("lines") or []).items():
            if not awb:
                continue
            first = lines[0]
            pickup, psrc, pev = w.pickup(awb, first, ref, c)
            if pickup is None:
                continue
            lane = c.lane_of(first.get("lane"))
            coded = [(ln, c.code_of(ln.get("description"))) for ln in lines]
            frt = [ln for ln, code in coded if code == FREIGHT]
            correct_frt = {}
            for ln in frt:
                cw = D(ln.get("quantity"))
                if cw is None:
                    continue
                res = pr.freight_sources(lane, pickup)
                if res["kind"] == "missing":
                    missing = {**res["missing"], "_inv": cites(cite(ref, ln))}
                    continue
                if res["kind"] == "unknown":
                    continue
                if res["kind"] == "conflict":
                    amts = []
                    for s in (res["a"], res["b"]):
                        a, e = pr.freight({**s, "msa_rows": (res["msa"] or {}).get("rows"),
                                           "msa_ref": (res["msa"] or {}).get("ref")}, cw)
                        amts.append((a, e, s))
                    if any(a is None for a, _, _ in amts):
                        continue
                    if amts[0][0] != amts[1][0]:
                        cf = conflicts.setdefault(awb, {"lines": [], "a": ZERO, "b": ZERO, "ev": [], "pickup": pickup,
                                                        "pev": pev, "clause": res.get("clause"),
                                                        "lane": (c.id, lane, c.period_of(pickup)),
                                                        "fuel": [x for x, k in coded if k == FUEL]})
                        cf["lines"].append(ln)
                        cf["a"], cf["b"] = cf["a"] + amts[0][0], cf["b"] + amts[1][0]
                        cf["ev"] += amts[0][1] + amts[1][1]
                        correct_frt[id(ln)] = None
                        continue
                    res = {"kind": "ok", "src": res["a"], "msa": res["msa"]}   # they agree: nothing to decide
                src = res["src"]
                amt, e = pr.freight({**src, "msa_rows": (res["msa"] or {}).get("rows"),
                                     "msa_ref": (res["msa"] or {}).get("ref")}, cw)
                if amt is None:
                    continue
                correct_frt[id(ln)] = amt
                billed = D(ln.get("amount"))
                if res.get("fb"):
                    flags["learned"] = res["fb"]
                if src["level"] > 2:
                    flags["levels"] = False
                if billed is not None and billed != amt:
                    flags["lanes"].add((c.id, lane, c.period_of(pickup)))
                    diffs.append(billed - amt)
                    ev += [cite(ref, ln)] + e + pev + [cite(c.ref, c.clause("pricing_date"), "text_src")]
                    flags["pickup"].add(psrc)
                    if src["kind"] in ("amendment", "annex") and src["received"] > copy["received"] \
                            and (src["eff"] or date.max) <= pickup:
                        flags["retro"] = True
            base_frt = None
            if frt and all(id(ln) in correct_frt for ln in frt):
                vals = [correct_frt[id(ln)] for ln in frt]
                base_frt = None if any(v is None for v in vals) else sum(vals, ZERO)
            once = set()
            for ln, code in coded:
                billed = D(ln.get("amount"))
                if billed is None or code in (None, FREIGHT) or code in NOT_PRICED:
                    continue
                correct, e = line_correct(w, pr, c, code, ln, awb, pickup, base_frt, lines)
                if correct is not None and code in once and c.acc.get(code, {}).get("unit") == "per_shipment":
                    correct = ZERO                  # a per-shipment fee billed a second time for the same shipment
                once.add(code)
                if correct is None:
                    continue
                if billed > correct:
                    diffs.append(billed - correct)
                    ev += [cite(ref, ln)] + e
                    flags["pickup"].add(psrc)       # every line is priced as at the pickup date
                    if any(x["doc"] == i["ref"] and i["received"] > copy["received"]
                           for x in e for i in w.instruments_for(c)):
                        flags["retro"] = True       # a term that arrived after the invoice made it wrong
                    if code == FUEL:
                        flags["lanes"].add((c.id, lane, c.period_of(pickup)))
                        ev += pev
        if missing:
            out.append(item("overcharge", {"invoice_id": inv_id}, "cannot_determine",
                            evidence=[missing.pop("_inv")] + [missing.pop("_cite")],
                            missing={k: v for k, v in missing.items() if not k.startswith("_") and k != "eff"},
                            contract=c, outbound=True, safe=True, lanes=flags["lanes"]))
            continue
        pos = sum((d for d in diffs if d > 0), ZERO)
        billed, by_lane, ev2 = ZERO, {}, []
        for cf in conflicts.values():
            pct, _ = pr.fuel_pct(cf["pickup"])
            fuel = cf["fuel"] if pct is not None else []
            billed += sum((D(x.get("amount")) or ZERO for x in cf["lines"] + fuel), ZERO)
            g = by_lane.setdefault(cf["lane"], [ZERO, ZERO])     # each lane is its own decision
            g[0] += cf["a"] + (rhu(pct / 100 * cf["a"]) if fuel else ZERO)
            g[1] += cf["b"] + (rhu(pct / 100 * cf["b"]) if fuel else ZERO)
            ev2 += [cite(ref, x) for x in cf["lines"]] + cf["ev"] + [cf["clause"]] + cf["pev"]
        hi_c, lo_c = sum((max(g) for g in by_lane.values()), ZERO), sum((min(g) for g in by_lane.values()), ZERO)
        if conflicts and billed > lo_c:         # billed below both answers: the ambiguity changes nothing
            ulo, fxev = to_usd(pr, f, max(ZERO, billed - hi_c) + pos)
            uhi, _ = to_usd(pr, f, billed - lo_c + pos)
            if ulo is None:
                out.append(missing_fx(pr, inv_id, "overcharge", ref, f))
            else:
                out.append(item("overcharge", {"invoice_id": inv_id}, "conflict", rng=(ulo, uhi), evidence=ev2 + fxev,
                                contract=c, outbound=False, lanes=set(by_lane), learned=flags["learned"],
                                currency=f.get("currency")))
            continue
        if pos <= 0:
            continue
        amount, fxev = to_usd(pr, f, pos)
        if amount is None:
            out.append(missing_fx(pr, inv_id, "overcharge", ref, f))
            continue
        out.append(item("overcharge", {"invoice_id": inv_id}, "finding", amount=amount, evidence=ev + fxev,
                        contract=c, outbound=True, pickup=flags["pickup"], lanes=flags["lanes"],
                        safe=flags["levels"] and flags["pickup"] <= {"tracking", "email"},
                        learned=flags["learned"], retro=flags["retro"], currency=f.get("currency"), native=pos))


def arith_problems(f: dict) -> list[str]:
    """Self-consistency of one extracted invoice: each priced line is quantity x rate (or base x percent), rounded half
    up, and the lines plus taxes add to the printed total. A failure means we misread a number (or the carrier
    mis-added it): the finding built on it deserves less confidence."""
    out, total = [], ZERO
    for ln in f.get("lines") or []:
        amt = D(ln.get("amount"))
        if amt is None:
            out.append(f"line without an amount: {ln.get('description')}")
            continue
        total += amt
        r = D(ln.get("rate"))
        base = D(ln.get("percent_base")) if ln.get("rate_is_percent") else D(ln.get("quantity"))
        if r is None or base is None or ln.get("late_fee"):
            continue
        want = rhu(base * r / 100) if ln.get("rate_is_percent") else rhu(base * r)
        if want != amt and abs(want - amt) > CENT:
            out.append(f"{ln.get('description')}: {base} x {r} = {want}, printed {amt}")
    for t in f.get("taxes") or []:
        total += D(t.get("amount")) or ZERO
    printed = D(f.get("total"))
    if printed is not None and abs(total - printed) > CENT:
        out.append(f"lines and taxes add to {total}, total printed {printed}")
    return out


def to_usd(pr: Pricer, f: dict, amount: Decimal):
    """Invoice currency -> USD, once per finding, at Annex F for the month of the invoice date."""
    if str(f.get("currency") or "USD").upper().startswith("USD"):
        return rhu(amount), []
    rate, ev = pr.fx(day(f.get("invoice_date")))
    if not rate:
        return None, []
    return rhu(amount / rate), ev


def missing_fx(pr: Pricer, inv_id: str, cls: str, ref: str, f: dict) -> dict:
    m = pr.fx_missing()
    return item(cls, {"invoice_id": inv_id}, "cannot_determine",
                evidence=[m.pop("_cite"), cite(ref, f, "date_src"), cite(ref, f, "number_src")],
                missing=m, contract=pr.c, outbound=True, safe=True)


def line_correct(w: World, pr: Pricer, c: Contract, code: str, ln: dict, awb: str, pickup: date,
                 base_frt: Decimal | None, lines: list[dict]):
    """What one non-freight line should have been -> (amount or None when we can't tell, evidence)."""
    qty = D(ln.get("quantity"))
    if code == FUEL:
        pct, ev = pr.fuel_pct(pickup)
        if pct is not None:
            rule = c.f.get("fuel_rule") or {}
            inc = set(rule.get("applies_to") or [FREIGHT])
            base = base_frt if FREIGHT in inc else ZERO
            if base is None:
                return None, []
            for part in lines:
                part_code = c.code_of(part.get("description"))
                if part_code in inc - {FREIGHT, FUEL}:
                    correct, pev = line_correct(w, pr, c, part_code, part, awb, pickup, base_frt, lines)
                    if correct is None:
                        return None, []
                    base += correct
                    ev += pev
            return rhu(pct / 100 * base), ev + cites(cite(c.ref, rule))
        # Some carriers bill fuel as a monetary rate per printed quantity, not as a percentage.
    fee, a, ev = pr.accessorial(code, pickup)
    if fee is None:
        return None, []
    unit = a.get("unit")
    months = a.get("only_months") or []
    if months and pickup.month not in months:
        return ZERO, ev
    evs = w.events.get(awb, [])
    if code == "DG" or code == "DRYICE":
        dg = w.dgds.get(awb)
        if not dg:
            return None, []
        items = dg[0].get("items") or []
        if not items:
            return None, []
        dry_only = all(x.get("refrigerant_only") or str(x.get("un_number") or "").upper().replace(" ", "")
                       in ("UN1845",) for x in items)
        has_dry = any(str(x.get("un_number") or "").upper().replace(" ", "") == "UN1845" for x in items)
        dgev = cites(cite(dg[1], items[0]), cite(c.ref, c.clause("dangerous_goods")))
        if code == "DG":
            return (ZERO if dry_only and c.clause("dangerous_goods").get("dry_ice_only_no_dg_fee", True) else fee), \
                ev + dgev
        return (fee if has_dry and not (not dry_only and any(
            c.code_of(x.get("description")) == "DG" for x in lines)) else ZERO), ev + dgev
    if code == "STOR" and unit == "per_day":
        arr = [e for e in evs if e["event"] == "ARRIVED_FACILITY"]
        rel = [e for e in evs if e["event"] in ("DELIVERED", "COLLECTED_BY_CONSIGNEE")]
        free = c.clause("storage").get("free_days")
        if arr and rel and free is not None:
            days = max(0, (rel[0]["date"] - arr[0]["date"]).days - int(free))
            return rhu(fee * days), ev + cites(cite(arr[0]["ref"], arr[0]["obj"]), cite(rel[0]["ref"], rel[0]["obj"]),
                                               cite(c.ref, c.clause("storage")))
        return (rhu(fee * qty) if qty is not None else None), ev
    if code == "DLV" and c.clause("delivery").get("only_if_carrier_delivers"):
        col = [e for e in evs if e["event"] == "COLLECTED_BY_CONSIGNEE"]
        dlv = [e for e in evs if e["event"] == "DELIVERED"]
        if col and not dlv:
            return ZERO, ev + cites(cite(col[0]["ref"], col[0]["obj"]), cite(c.ref, c.clause("delivery")))
        return fee, ev
    if code == "CUSTOMS" and c.clause("customs").get("shipper_pays_unless_carrier_error"):
        holds = [e for e in evs if e["event"] == "CUSTOMS_HOLD" or e.get("fault") == "carrier"]
        if any(e.get("fault") == "carrier" for e in holds):
            h = next(e for e in holds if e.get("fault") == "carrier")
            return ZERO, ev + cites(cite(h["ref"], h["obj"]), cite(c.ref, c.clause("customs")))
        return fee, ev
    if unit == "per_shipment":
        return fee, ev
    if unit in ("per_kg", "per_lb", "per_cwt", "per_piece", "per_container", "per_day", "per_month"):
        return (rhu(fee * qty) if qty is not None else None), ev
    return None, []


def duplicates(w: World, out: list[dict]):
    seen: dict[tuple, list] = defaultdict(list)       # (awb or period, code, amount) -> [(date, inv, line, ref)]
    copies = invoice_copies(w)
    for inv_id, (copy, all_copies) in copies.items():
        f = copy["f"]
        c = w.contract_for(f.get("contract_id"))
        codes = c.codes if c else {}
        for ln in f.get("lines") or []:
            code = code_from(codes, ln.get("description")) if codes else None
            code = code or norm_label(re.sub(r"\([^)]*\)", " ", str(ln.get("description"))))
            if code in (LATE, "TAX") or ln.get("late_fee"):
                continue
            ident = norm_awb(ln.get("awb")) or (f"period:{ln.get('period')}" if ln.get("period") else "")
            if not ident:
                continue
            seen[(ident, code, D(ln.get("amount")))].append(
                (day(f.get("invoice_date")) or date.min, copy["received"], inv_id, ln, copy["ref"], c, f))
    dup_lines: dict[str, list] = defaultdict(list)
    for k, hits in seen.items():
        if len(hits) < 2 or k[2] is None:
            continue
        hits.sort(key=lambda h: (h[0], h[1], h[2]))
        for h in hits[1:]:
            once = h[3].get("awb") and h[5] and h[5].acc.get(k[1], {}).get("unit") == "per_shipment"
            if h[2] != hits[0][2] or not once:
                dup_lines[h[2]].append((h, hits[0]))
    for inv_id, pairs in dup_lines.items():
        h0 = pairs[0][0]
        c, f, ref = h0[5], h0[6], h0[4]
        amount = sum((D(h[3]["amount"]) for h, _ in pairs), ZERO)
        credited = credit_against(w, [p[1][2] for p in pairs], [h[3] for h, _ in pairs])
        amount -= credited
        if amount <= 0:
            continue
        ev = []
        for h, first in pairs:
            ev += [cite(first[4], first[3]), cite(h[4], h[3])]
        pr = Pricer(w, c, inv_id) if c else None
        usd, fxev = to_usd(pr, f, amount) if pr else (rhu(amount), [])
        if usd is None:
            out.append(missing_fx(pr, inv_id, "duplicate", ref, f))
            continue
        alias = [cite(c.ref, x) for x in (c.f.get("charge_codes") or []) if c and
                 any(norm_label(a) in norm_label(h[3].get("description")) for h, _ in pairs
                     for a in (x.get("aliases") or []))] if c else []
        out.append(item("duplicate", {"invoice_id": inv_id}, "finding", amount=usd, evidence=ev + alias + fxev,
                        contract=c, outbound=True, safe=True, currency=f.get("currency"), native=amount))
    # the same invoice number sent again, asking to be paid again after the ledger shows it paid
    for inv_id, (copy, all_copies) in copies.items():
        if inv_id in dup_lines or len(all_copies) < 2:
            continue
        row, rref = w.register_row(inv_id)
        if not row or row.get("status") not in ("paid", "scheduled"):
            continue
        paid = day(row.get("paid_date"))
        for again in all_copies[1:]:
            asks = str(again["email"].get("requests_payment")).lower() == "yes" and not yes(again["f"].get("marked_copy"))
            if not asks or (row.get("status") == "paid" and paid and paid > again["received"]):
                continue
            f = again["f"]
            c = w.contract_for(f.get("contract_id"))
            total = D(f.get("total"))
            row, rref = w.register_row(inv_id, lambda r: r.get("status") in ("paid", "scheduled")) or (row, rref)
            if total is None:
                continue
            usd, fxev = to_usd(Pricer(w, c, inv_id), f, total) if c else (rhu(total), [])
            if usd is None:
                out.append(missing_fx(Pricer(w, c, inv_id), inv_id, "duplicate", again["ref"], f))
                break
            out.append(item("duplicate", {"invoice_id": inv_id}, "finding", amount=usd,
                            evidence=[cite(rref, row), email_line(w, again, inv_id),
                                      cite(again["ref"], f, "total_src"),
                                      cite(copy["ref"], copy["f"], "number_src")] + fxev,
                            contract=c, outbound=True, safe=True, currency=f.get("currency"), native=total,
                            resend=True))
            break


def email_line(w: World, doc: dict, inv_id: str) -> dict | None:
    """The sending email's own body line that asks for payment (or names the invoice), as a citation."""
    email = next((x for x in w.email_docs.get(doc["meta"]["email"], [])), None)
    for p, text in enumerate((email or {}).get("pages") or [], 1):
        lines = [ln.strip() for ln in text.split("\n")]
        body = lines[lines.index("") + 1:] if "" in lines else lines
        for ln in body:
            if len(ln.replace(" ", "")) >= 12 and re.search(r"payment|pay |outstanding|" + re.escape(inv_id), ln, re.I):
                return {"doc": doc["meta"]["email"], "page": p, "quote": ln}
    return None


def credit_against(w: World, inv_ids: list[str], lines: list[dict]) -> Decimal:
    """Credit notes that reverse what was billed twice: credited in full leaves nothing, in part reduces it. A credit
    note with lines counts only its lines on the duplicated waybills; one without counts when it names the invoice."""
    total = ZERO
    awbs = {norm_awb(ln.get("awb")) for ln in lines} - {""}
    for cn in w.credit_notes:
        if cn.get("credit_type") not in ("dispute", "other", None):
            continue
        cls = cn.get("lines") or []
        hit = [x for x in cls if norm_awb(x.get("awb")) in awbs]
        if hit:
            # Cancellations print negative, some carriers print credits positive; a note mixing both counts net.
            amts = [D(x.get("amount")) or ZERO for x in hit]
            total += sum((abs(a) for a in amts), ZERO) if all(a <= 0 for a in amts) or all(a >= 0 for a in amts) \
                else max(ZERO, -sum(amts, ZERO))
        elif not any(x.get("awb") for x in cls) and {norm_inv(x) for x in cn.get("invoice_numbers") or []} & set(inv_ids):
            total += abs(D(cn.get("total")) or ZERO)
    return total


def waived(w: World, out: list[dict]):
    per_inv: dict[str, dict] = {}
    for inv_id, (copy, _) in invoice_copies(w).items():
        f, ref = copy["f"], copy["ref"]
        c = w.contract_for(f.get("contract_id"))
        if c is None:
            continue
        for ln in f.get("lines") or []:
            code = c.code_of(ln.get("description"))
            awb = norm_awb(ln.get("awb"))
            when = day(ln.get("date")) or day(f.get("invoice_date"))
            for wv in w.waivers:
                name = wv.get("written_by_name") or wv["meta"]["from_name"]
                one_off = one_off_for(w, c, inv_id, name, ln.get("period") or (month_of(when) if when else None),
                                      c.lane_of(ln.get("lane"))) \
                    if code and c.code_loose(wv.get("charge")) == code else None
                if not (wv_covers(c, wv, code, awb, ln, when, inv_id) or one_off) or w.aimed_at_machines(wv["ref"], wv):
                    continue
                st = per_inv.setdefault(inv_id, {"amt": ZERO, "ev": [], "f": f, "ref": ref, "c": c, "verdicts": set(),
                                                 "missing": None, "learned": None})
                email = wv.get("written_by_email") or (None if yes(wv.get("quoted_only")) else wv["meta"]["from_addr"])
                ok, sch, fb = w.authorised(c, "waivers", email, name, day(wv.get("written_on")) or wv["received"],
                                           c.carrier, {inv_id}, lane=c.lane_of(ln.get("lane")))
                if one_off:
                    ok, fb = True, one_off
                if yes(wv.get("quoted_only")) and not original_held(w, wv) and not one_off:
                    p = next((p for p in c.persons if "waivers" in (p.get("categories") or [])
                              and norm_label(p.get("name")) == norm_label(name)), None)
                    holder = {"party": p.get("party"), "person": p.get("name"), "role": p.get("title")} if p \
                        else c.kam()
                    st["missing"] = {"name": f"original waiver email from {name}" + (f" dated {wv['written_on']}" if
                                                                             wv.get("written_on") else "")
                                     + f" waiving the {wv.get('charge')}" + (f" on AWB {awb}" if awb else ""),
                                     "holder": holder}
                    st["ev"] += [cite(wv["ref"], wv), cite(ref, ln)]
                    continue
                if not ok:
                    continue
                if not condition_met(w, wv):
                    continue
                billed = D(ln.get("amount")) or ZERO
                share = billed
                if D(wv.get("waived_amount")) is not None:
                    share = min(billed, D(wv["waived_amount"]))
                elif D(wv.get("waived_percent")) is not None:
                    share = rhu(billed * D(wv["waived_percent"]) / 100)
                st["amt"] += share
                st["ev"] += [cite(wv["ref"], wv), sch, cite(ref, ln), cite(c.ref, c.clause("waiver"))]
                st["learned"] = st["learned"] or fb
                break
    for inv_id, st in per_inv.items():
        if st["amt"] > 0:
            usd, fxev = to_usd(Pricer(w, st["c"], inv_id), st["f"], st["amt"])
            if usd is None:
                out.append(missing_fx(Pricer(w, st["c"], inv_id), inv_id, "waived_but_billed", st["ref"], st["f"]))
                continue
            out.append(item("waived_but_billed", {"invoice_id": inv_id}, "finding", amount=usd,
                            evidence=st["ev"] + fxev, contract=st["c"], outbound=True, safe=True,
                            learned=st["learned"], currency=st["f"].get("currency"), native=st["amt"]))
        elif st["missing"]:
            out.append(item("waived_but_billed", {"invoice_id": inv_id}, "cannot_determine", evidence=st["ev"],
                            missing=st["missing"], contract=st["c"], outbound=True, safe=True))


def wv_covers(c: Contract, wv: dict, code: str | None, awb: str, ln: dict, when: date | None, inv_id=None) -> bool:
    """A waiver covers exactly the charge and the shipments or months it names (clause 11.2)."""
    if code is None or c.code_loose(wv.get("charge")) != code:
        return False
    awbs = {norm_awb(a) for a in wv.get("awbs") or []}
    months = set(wv.get("months") or [])
    invoices = {norm_inv(n) for n in wv.get("invoice_numbers") or []}
    per = ln.get("period") or (month_of(when) if when else None)
    return bool(awbs or months or invoices) and (not awbs or awb in awbs) and (not months or per in months) \
        and (not invoices or inv_id in invoices)


def one_off_for(w: World, c: Contract, inv_id: str, person: str, month: str | None, lane=None) -> dict | None:
    for fb in reversed(w.feedback):
        fact = fb.get("fact") or {}
        if fb.get("decision") == "one_off_approval" and fact.get("type") == "waiver_scope" and \
                norm_label((fact.get("subject") or {}).get("person")) == norm_label(person) and feedback_matches(
                    fb, None, contract_id=c.id, lane=lane, person=person, party=c.carrier, category="waivers",
                    invoice_id=inv_id, period=month, **{"class": "waived_but_billed"}):
            if (fact.get("subject") or {}).get("party") not in (None, c.carrier):
                continue
            months = (fact.get("value") or {}).get("months") or []
            if not months or month in months:
                return fb
    return None


def original_held(w: World, wv: dict) -> bool:
    """A quoted waiver counts once we hold its author's own email saying the same."""
    for o in w.waivers:
        if o is wv or yes(o.get("quoted_only")):
            continue
        if norm_label(o["meta"]["from_name"]) == norm_label(wv.get("written_by_name")) and \
                set(map(norm_awb, o.get("awbs") or [])) == set(map(norm_awb, wv.get("awbs") or [])) and \
                set(o.get("months") or []) == set(wv.get("months") or []) and \
                norm_label(o.get("charge")) == norm_label(wv.get("charge")):
            return True
    return False


def condition_met(w: World, wv: dict) -> bool:
    if not wv.get("condition"):
        return True
    by = day(wv.get("condition_pay_by"))
    if by and wv.get("condition_invoice"):
        row, _ = w.register_row(norm_inv(wv["condition_invoice"]))
        paid = day((row or {}).get("paid_date"))
        return bool(paid and paid <= by)
    return False                      # a condition we can't check is not met


def late_fees(w: World, out: list[dict], disputed: dict[str, Decimal]):
    """Late fees billed on an invoice that wasn't late enough to owe them. Every fee billed on one target invoice, on
    whichever invoices carry it, is measured against what the target owes in total; the excess lands on the invoice
    that billed it (so month 2 billed on its own later invoice is caught)."""
    by_tgt: dict[str, list] = defaultdict(list)
    for inv_id, (copy, _) in invoice_copies(w).items():
        c = w.contract_for(copy["f"].get("contract_id"))
        if c is None:
            continue
        lines: dict[str, list] = defaultdict(list)
        for ln in copy["f"].get("lines") or []:
            tgt = norm_inv((ln.get("late_fee") or {}).get("on_invoice"))
            if tgt and (ln.get("late_fee") or c.code_of(ln.get("description")) == LATE):
                lines[tgt].append(ln)
        for tgt, lns in lines.items():
            by_tgt[tgt].append((day(copy["f"].get("invoice_date")) or date.min, inv_id, copy, c, lns))
    per_inv: dict[str, dict] = {}
    for tgt, bills in by_tgt.items():
        row, rref = w.register_row(tgt)
        paid = day((row or {}).get("paid_date"))
        if not paid:
            continue
        bills.sort(key=lambda b: (b[0], b[1]))
        printed = D((bills[0][4][0].get("late_fee") or {}).get("overdue_amount"))
        base = printed if printed is not None else D(row.get("amount"))
        principal = base - disputed.get(tgt, ZERO) if base is not None else None
        cum = ZERO
        for _, inv_id, copy, c, lns in bills:
            due, xev, fb = w.due_date(tgt, c, day(row.get("due_date")), {inv_id}, "late_fee_not_owed")
            sched = fee_schedule(c, principal, due, paid)
            billed = sum((D(ln.get("amount")) or ZERO for ln in lns), ZERO)
            months = [(ln.get("late_fee") or {}).get("month_number") for ln in lns]
            if sched is None:
                extra = ZERO
            elif all(isinstance(m, int) and m > 0 for m in months):      # each billed month against its own fee
                extra = billed - sum((sched[m - 1] if m <= len(sched) else ZERO for m in months), ZERO)
            else:
                owed = sum(sched, ZERO)
                extra = max(ZERO, cum + billed - owed) - max(ZERO, cum - owed)
            cum += billed
            if extra <= 0:
                continue
            st = per_inv.setdefault(inv_id, {"amt": ZERO, "ev": [], "fb": None, "copy": copy, "c": c})
            st["amt"] += extra
            st["ev"] += [cite(copy["ref"], ln) for ln in lns] + [cite(rref, row)] + xev + \
                [cite(c.ref, c.clause("late_fee"))] + ([cite(c.ref, c.clause("extension"))] if xev else [])
            st["fb"] = st["fb"] or fb
    for inv_id, st in per_inv.items():
        f, c = st["copy"]["f"], st["c"]
        usd, fxev = to_usd(Pricer(w, c, inv_id), f, st["amt"])
        if usd is None:
            out.append(missing_fx(Pricer(w, c, inv_id), inv_id, "late_fee_not_owed", st["copy"]["ref"], f))
            continue
        out.append(item("late_fee_not_owed", {"invoice_id": inv_id}, "finding", amount=usd, evidence=st["ev"] + fxev,
                        contract=c, outbound=True, safe=True, learned=st["fb"], currency=f.get("currency"),
                        native=st["amt"]))


def fee_schedule(c: Contract, principal: Decimal | None, due: date | None, paid: date) -> list[Decimal] | None:
    """Each month's fee owed: none within grace, else ceil(days late / 30) months on a simple or compounding base."""
    lf = c.clause("late_fee")
    r, grace = D(lf.get("percent_per_month")), lf.get("grace_days") or 0
    per = lf.get("days_per_month") or 30
    if r is None or due is None or principal is None:
        return None
    late = (paid - due).days
    if late <= int(grace):
        return []
    fees, base = [], principal
    for _ in range(math.ceil(late / per)):
        fees.append(rhu(r / 100 * base))
        if lf.get("compounding"):
            base += fees[-1]
    return fees


def late_fee_risk(w: World, out: list[dict], disputed: dict[str, Decimal]):
    seen = set()
    for reg in sorted(w.registers, key=lambda r: r["received"], reverse=True):
        for row in reg.get("rows") or []:
            inv_id = norm_inv(row.get("invoice_number"))
            if inv_id in seen:
                continue
            seen.add(inv_id)
            if row.get("status") not in ("open", "disputed") or day(row.get("paid_date")):
                continue
            copy = (w.invoices.get(inv_id) or [None])[0]
            f = copy["f"] if copy else {"currency": row.get("currency"), "invoice_date": row.get("invoice_date")}
            c = w.contract_for(f.get("contract_id")) or (next(iter(w.contracts.values())) if len(w.contracts) == 1
                                                         else None)
            if c is None:
                continue
            due, xev, fb = w.due_date(inv_id, c, day(row.get("due_date")), {inv_id}, "late_fee_risk")
            if due is None or not 0 < (due - w.as_of).days <= 14:
                continue
            r = D(c.clause("late_fee").get("percent_per_month"))
            principal = D(row.get("amount"))
            if r is None or principal is None:
                continue
            principal = max(ZERO, principal - disputed.get(inv_id, ZERO))
            fee = rhu(r / 100 * principal)
            usd, fxev = to_usd(Pricer(w, c), {**f, "currency": row.get("currency") or f.get("currency")}, fee)
            if usd is None:
                continue
            out.append(item("late_fee_risk", {"invoice_id": inv_id}, "finding", amount=usd, deadline=due,
                            evidence=[cite(reg["ref"], row), cite(c.ref, c.clause("late_fee"))] + xev + fxev,
                            contract=c, outbound=False, learned=fb))


def tier_rebate(rb: dict, spend: Decimal) -> Decimal:
    """The rebate a spend earns: the reached tier's rate on all of it, or band by band when the contract says so."""
    ts = sorted(rb.get("tiers") or [], key=lambda t: D(t["min_spend"]))
    if rb.get("whole_spend_at_tier") is False:
        return rhu(sum((D(t["percent"]) / 100 * ((min(spend, D(n["min_spend"])) if n else spend) - D(t["min_spend"]))
                        for t, n in zip(ts, ts[1:] + [None]) if spend > D(t["min_spend"])), ZERO))
    return rhu(max((D(t["percent"]) for t in ts if spend >= D(t["min_spend"])), default=ZERO) / 100 * spend)


def fx_unknown(w: World, c: Contract, key: dict, ev: list) -> dict:
    """A cashback key whose INR amounts have no exchange rate for their month: ask Treasury for it."""
    m = Pricer(w, c).fx_missing()
    return item("cashback", key, "cannot_determine", evidence=[m.pop("_cite")] + ev, missing=m, contract=c,
                outbound=True, safe=True)


def cashback(w: World, out: list[dict]):
    for c in w.contracts.values():
        qs_cl = c.clause("qualifying_spend")
        rb, ic, oc = c.clause("rebate"), c.clause("improvement_credit"), c.cl.get("other_credits") or []
        tiers = bool(rb.get("applies") and rb.get("tiers"))
        annual = tiers and rb.get("period") == "contract_year"   # an annual rebate is a promised credit per year
        end = term_end(w, c)
        for q in (quarters(c, w.as_of, end) if tiers and not annual else []):
            first, last = quarter_bounds(q)
            spend, ev, ok = qualifying(w, c, first, last)
            if ok is None:
                out.append(fx_unknown(w, c, {"contract_id": c.id, "period": q, "credit_type": "rebate"},
                                      [cite(c.ref, qs_cl)]))
            earned = tier_rebate(rb, spend) if ok else ZERO
            if earned > 0:
                add_credit(w, out, c, q, "rebate", earned, last + timedelta(days=int(rb.get("credit_due_days") or 0)),
                           ev + [cite(c.ref, rb), cite(c.ref, qs_cl)])
        if not ((ic.get("applies") and D(ic.get("percent"))) or oc or annual) or not c.effective:
            continue
        n = 1
        while (b := add_years(c.effective, n) - timedelta(days=1)) < w.as_of and \
                not (end and add_years(c.effective, n - 1) > end):
            applicable = [o for o in oc if not o.get("contract_years") or n in o["contract_years"]]
            needs_spend = annual or bool(ic.get("applies") and D(ic.get("percent"))) or any(
                D(o.get("amount")) is None and D(o.get("percent")) is not None for o in applicable)
            spend, ev, ok = qualifying(w, c, add_years(c.effective, n - 1), b) if needs_spend else (ZERO, [], True)
            if ok is None:
                out.append(fx_unknown(w, c, {"contract_id": c.id, "period": f"CY{n}", "credit_type": "promised_credit"},
                                      [cite(c.ref, qs_cl)]))
            else:
                anniv = lambda x: 1 + int(x or 0)          # due days counted from the anniversary
                parts = ([(rhu(D(ic["percent"]) / 100 * spend), anniv(ic.get("due_days_after_anniversary")), ic)]
                         if ic.get("applies") and D(ic.get("percent")) else []) + \
                        ([(tier_rebate(rb, spend), int(rb.get("credit_due_days") or 0), rb)] if annual else []) + \
                        [(D(o["amount"]) if D(o.get("amount")) is not None else
                          rhu((D(o.get("percent")) or ZERO) / 100 * spend),
                          anniv(o.get("due_days_after_anniversary")), o) for o in applicable]
                earned = sum((p[0] for p in parts), ZERO)
                if earned > 0:
                    due = b + timedelta(days=max(p[1] for p in parts))
                    add_credit(w, out, c, f"CY{n}", "promised_credit", earned, due,
                               ev + [cite(c.ref, p[2]) for p in parts] + [cite(c.ref, qs_cl)])
            n += 1


def term_end(w: World, c: Contract) -> date | None:
    """The contract's end date, moved by any signed extension."""
    ends = [x for x in [c.end] + [day(i.get("new_end_date")) for i in w.instruments_for(c) if i.get("signed_by_both")]
            if x]
    return max(ends) if ends else None


def quarters(c: Contract, as_of: date, end: date | None = None) -> list[str]:
    """Every calendar quarter of the term that has closed by as_of."""
    out, d = [], c.effective
    while d and d <= as_of and (not end or d <= end):
        q = quarter_of(d)
        if quarter_bounds(q)[1] < as_of:
            out.append(q)
        d = quarter_bounds(q)[1] + timedelta(days=1)
    return out


def contract_invoices(w: World, c: Contract, first: date, last: date) -> list[dict]:
    """This contract's invoices dated in [first, last], oldest first."""
    hits = [(day(cp["f"].get("invoice_date")), k, cp) for k, (cp, _) in invoice_copies(w).items()
            if str(cp["f"].get("contract_id") or "").upper() == c.id.upper()]
    return [cp for d, _, cp in sorted(h for h in hits if h[0] and first <= h[0] <= last)]


def invoice_spend(c: Contract, pr: Pricer, f: dict) -> Decimal | None:
    """One invoice's Qualifying Spend in USD, as the contract defines it; None when its INR has no exchange rate."""
    cl = c.clause("qualifying_spend")
    inc = {x.upper() for x in cl.get("include_codes") or []} or {FREIGHT, FUEL, SECURITY}
    exc = {x.upper() for x in cl.get("exclude_codes") or []} | {"TAX", "DUTY", LATE}
    s = ZERO
    for ln in f.get("lines") or []:
        code = c.code_of(ln.get("description"))
        if code is None or code in exc or ln.get("late_fee"):
            continue
        if code in inc or (cl.get("include_annex_d", True) and code in c.acc):
            s += D(ln.get("amount")) or ZERO
    if str(f.get("currency") or "USD").upper().startswith("INR"):
        rate, _ = pr.fx(day(f.get("invoice_date")))
        return rhu(s / rate) if rate else (ZERO if not s else None)
    return s


def qualifying(w: World, c: Contract, first: date, last: date):
    """Qualifying Spend in USD for invoices dated in [first, last], as the contract defines it."""
    cl = c.clause("qualifying_spend")
    pr = Pricer(w, c)
    spend, ev, any_inv = ZERO, [], False
    for cp in contract_invoices(w, c, first, last):
        s = invoice_spend(c, pr, cp["f"])
        if s is None:
            return ZERO, [], None
        spend += s
        ev += spend_evidence(c, pr, cp)
        any_inv = True
    for cn in spend_credits(w, c, first, last):
        amt = credit_spend(c, pr, cn)
        if amt is None:
            return ZERO, [], None
        spend -= amt
        ev += cites(cite(cn["ref"], cn, "total_src"))
    return spend, ev, any_inv


def spend_evidence(c: Contract, pr: Pricer, cp: dict) -> list[dict]:
    f = cp["f"]
    ev = cites(*[cite(cp["ref"], ln) for ln in f.get("lines") or []])
    if str(f.get("currency") or "USD").upper().startswith("INR"):
        ev += pr.fx(day(f.get("invoice_date")))[1]
    return ev


def spend_credits(w: World, c: Contract, first: date, last: date) -> list[dict]:
    if not c.clause("qualifying_spend").get("net_of_credit_notes", True):
        return []
    return [cn for cn in w.credit_notes if str(cn.get("contract_id") or "").upper() == c.id.upper() and
            day(cn.get("credit_date")) and first <= day(cn["credit_date"]) <= last and
            cn.get("credit_type") not in ("rebate", "improvement")]


def credit_spend(c: Contract, pr: Pricer, cn: dict) -> Decimal | None:
    # Credit totals may include taxes and duty. When coded lines are supplied, only eligible lines reduce spend.
    lines = cn.get("lines") or []
    if lines and all(c.code_of(ln.get("description")) is not None for ln in lines):
        f = {"lines": [{**ln, "amount": str(abs(D(ln.get("amount")) or ZERO))} for ln in lines],
             "currency": cn.get("currency"), "invoice_date": cn.get("credit_date")}
        return invoice_spend(c, pr, f)
    amt = abs(D(cn.get("total")) or ZERO)
    if str(cn.get("currency") or "USD").upper().startswith("INR"):
        rate, _ = pr.fx(day(cn.get("credit_date")))
        return rhu(amt / rate) if rate else (ZERO if not amt else None)
    return amt


def margin_step_down(w: World, out: list[dict]):
    """A handling margin that should have stepped down: once a quarter's Qualifying Spend reaches the threshold, every
    later invoice that quarter is due the lower rate. One cashback item per quarter, never per-invoice overcharges."""
    for c in w.contracts.values():
        hm = c.clause("handling_margin")
        hi, lo, cap = D(hm.get("percent")), D(hm.get("step_down_percent")), D(hm.get("step_down_after_spend"))
        if not hm.get("applies") or None in (hi, lo, cap) or lo >= hi:
            continue
        pr = Pricer(w, c)
        for q in quarters(c, w.as_of, term_end(w, c)):
            first, last = quarter_bounds(q)
            run, crossed, over, ev, unknown, gap = ZERO, False, ZERO, [], False, False
            credits = sorted(spend_credits(w, c, first, last), key=lambda cn: day(cn["credit_date"]))
            for cp in contract_invoices(w, c, first, last):
                while credits and day(credits[0]["credit_date"]) <= day(cp["f"]["invoice_date"]):
                    cn = credits.pop(0)
                    reduction = credit_spend(c, pr, cn)
                    gap, run = gap or reduction is None, run - (reduction or ZERO)
                    ev += cites(cite(cn["ref"], cn, "total_src"))
                lines = cp["f"].get("lines") or []
                unknown = unknown or (gap and not crossed and any(c.code_of(x.get("description")) == "MARGIN"
                                                                  for x in lines))
                if crossed:                     # the crossing invoice itself keeps the old rate
                    extra = ZERO
                    for ln in lines:
                        if c.code_of(ln.get("description")) != "MARGIN":
                            continue
                        awb = norm_awb(ln.get("awb"))
                        base = D(ln.get("percent_base")) or sum(
                            (D(x.get("amount")) or ZERO for x in lines if c.code_of(x.get("description")) == "COST"
                             and norm_awb(x.get("awb")) == awb), ZERO)
                        extra += max(ZERO, (D(ln.get("amount")) or ZERO) - rhu(lo / 100 * base))
                        ev.append(cite(cp["ref"], ln))
                    usd = to_usd(pr, cp["f"], extra)[0] if extra else ZERO
                    unknown, over = unknown or usd is None, over + (usd or ZERO)
                spend = invoice_spend(c, pr, cp["f"])
                gap, run = gap or spend is None, run + (spend or ZERO)
                crossed = crossed or (not gap and run >= cap)
                ev += spend_evidence(c, pr, cp)
            if unknown:
                out.append(fx_unknown(w, c, {"contract_id": c.id, "period": q, "credit_type": "margin_step_down"},
                                      [cite(c.ref, hm)]))
            elif over > 0:                      # ponytail: claimable at quarter end; a contract credit date goes here
                add_credit(w, out, c, q, "margin_step_down", over, last,
                           ev + [cite(c.ref, hm), cite(c.ref, c.clause("qualifying_spend"))])


def add_credit(w: World, out: list[dict], c: Contract, period: str, ctype: str, earned: Decimal, due: date, ev):
    kinds = {"rebate": ("rebate",), "promised_credit": ("improvement", "other", "rebate"),
             "margin_step_down": ("margin",)}[ctype]
    key = {"contract_id": c.id, "period": period, "credit_type": ctype}
    credited, cev = ZERO, []
    for cn in w.credit_notes:
        if str(cn.get("contract_id") or "").upper() == c.id.upper() and cn.get("credit_type") in kinds and \
                str(cn.get("period") or "").replace(" ", "").upper() == period.upper():
            amt = abs(D(cn.get("total")) or ZERO)
            if str(cn.get("currency") or "USD").upper().startswith("INR"):
                rate, _ = Pricer(w, c).fx(day(cn.get("credit_date")))
                if not rate:
                    out.append(fx_unknown(w, c, key, ev + [cite(cn["ref"], cn, "total_src")]))
                    return
                amt = rhu(amt / rate)
            credited += amt
            cev.append(cite(cn["ref"], cn, "total_src"))
    owed = earned - credited
    if owed <= 0:
        return
    out.append(item("cashback", key, "finding", amount=owed, evidence=ev + cev, contract=c, outbound=w.as_of >= due,
                    safe=True, due=due))


def expiry(w: World, out: list[dict]):
    for c in w.contracts.values():
        when, ev = c.end, [cite(c.ref, c.f, "end_src")]
        if c.f.get("auto_renews"):
            nd = day(c.f.get("notice_deadline"))
            if not nd and c.end and c.f.get("renewal_notice_days"):
                nd = c.end - timedelta(days=int(c.f["renewal_notice_days"]))
            when, ev = nd or when, ev + [cite(c.ref, c.f, "renewal_src")]
        for i in w.instruments_for(c):
            nd, end = day(i.get("new_notice_deadline")), day(i.get("new_end_date"))
            if i.get("signed_by_both") and (nd or end):
                if not nd and c.f.get("auto_renews") and c.f.get("renewal_notice_days"):
                    nd = end - timedelta(days=int(c.f["renewal_notice_days"]))
                when, ev = nd or end, [cite(i["ref"], i, "term_src"), cite(c.ref, c.f, "renewal_src")]
        if when and 0 <= (when - w.as_of).days <= 60:
            out.append(item("expiry", {"contract_id": c.id}, "finding", deadline=when, evidence=ev, contract=c,
                            outbound=False))


# ---- feedback that closes, rejects or overrides a key -----------------------------------------------------------------
def apply_decisions(w: World, items: list[dict], drop=("close", "reject")) -> list[dict]:
    keep = []
    for it in items:
        k = key_of(it)
        gone = False
        for fb in w.feedback:
            fk = fb.get("finding_key") or {}
            fkey = {**fk, "class": fk.get("class") or ("cashback" if "period" in fk else "expiry")}
            fkey["invoice_id"] = norm_inv(fk.get("invoice_id")) if fk.get("invoice_id") else None
            if key_of({**fkey, **({"invoice_id": fkey["invoice_id"]} if fkey.get("invoice_id") else {})}) != k:
                continue
            c = it.get("_contract")
            copy = (w.invoices.get(it.get("invoice_id")) or [None])[0]
            lines = copy["f"].get("lines") or [] if copy else []
            lanes = {c.lane_of(ln.get("lane")) if c else norm_lane(ln.get("lane")) for ln in lines} - {""}
            dates = [w.pickup(norm_awb(ln.get("awb")), ln, copy["ref"], c)[0] for ln in lines] if copy else []
            on = next((d for d in dates if d), day(copy["f"].get("invoice_date")) if copy else None)
            fact = fb.get("fact") or {}
            person = (fact.get("value") or {}).get("sent_by")
            context = {"contract_id": c.id if c else it.get("contract_id"), "party": c.carrier if c else None,
                       "lane": next(iter(lanes)) if len(lanes) == 1 else None, "person": person,
                       "category": {"overcharge": "rates", "waived_but_billed": "waivers",
                                    "late_fee_not_owed": "payment_terms", "late_fee_risk": "payment_terms"}.get(it["class"]),
                       **{field: it.get(field) for field in ("invoice_id", "class", "period", "credit_type")}}
            if not feedback_matches(fb, on, **context):
                continue
            dec = fb.get("decision")
            if dec in drop:
                gone = True
            elif dec == "override_amount":
                amt = D(((fb.get("fact") or {}).get("value") or {}).get("amount_usd"))
                if amt is not None:
                    gone = False
                    it["verdict"], it["amount_usd"], it["amount_range"] = "finding", money(amt), None
                    it["_outbound"] = True
                    it["_learned"] = fb
                    if str(it.get("_currency") or "USD").upper().startswith("USD"):
                        it["_native"] = amt
        if not gone:
            keep.append(it)
    return keep


# ---- actions, confidence, ids -----------------------------------------------------------------------------------------
def finish(w: World, items: list[dict], mode: str, prev: dict) -> list[dict]:
    per_target: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for it in items:
        if it["verdict"] == "finding" and it.get("_outbound") and it["amount_usd"]:
            per_target[it.get("invoice_id") or key_of(it)] += Decimal(it["amount_usd"])
    open_lanes = set()
    for it in items:
        if it["verdict"] == "conflict":
            open_lanes |= set(it.get("_lanes") or ())
    for it in items:
        it["_safe"] = unattended_ready(w, it)
        it["action"] = action(it, mode, per_target[it.get("invoice_id") or key_of(it)], open_lanes)
        it["confidence"] = confidence(it)
        if it.get("_learned"):
            it["learned_at"] = learned(it["_learned"])
    for it in items:
        k = key_of(it)
        old = prev.get(k)
        base = k
        sig = [it["verdict"], it["amount_usd"], list(it["amount_range"] or [])]
        if old and old["sig"] == sig:
            it["id"] = old["id"]
        elif old:
            n = int(old["id"].rsplit("#v", 1)[1]) + 1 if "#v" in old["id"] else 2
            it["id"], it["supersedes"] = f"{base}#v{n}", [old["id"]]
        else:
            it["id"] = base
            if it.get("_retro"):
                it["retro"] = True
    return items


def unattended_ready(w: World, it: dict) -> bool:
    """Apply evidence, pickup and recipient checks to every outbound detector, not just repricing."""
    c = it.get("_contract")
    if not it.get("_safe") or not c or c.f.get("signed_by_both") is False or not it["evidence"]:
        return False
    if it["verdict"] == "cannot_determine":
        holder = (it.get("missing_doc") or {}).get("holder") or {}
        return any(p.get("email") and p.get("party") == holder.get("party") and
                   norm_label(p.get("name")) == norm_label(holder.get("person")) for p in c.persons)
    if not c.person("billing_disputes", c.carrier) or not c.person("billing_disputes", c.carrier).get("email"):
        return False
    copies = [invoice_copies(w)[it["invoice_id"]][0]] if it.get("invoice_id") in w.invoices else []
    if it["class"] == "cashback":
        period = it["period"]
        first, last = quarter_bounds(period) if "-Q" in period else (
            add_years(c.effective, int(period[2:]) - 1), add_years(c.effective, int(period[2:])) - timedelta(days=1))
        copies = contract_invoices(w, c, first, last)
    for cp in copies:
        if norm_inv(cp["f"]["invoice_number"]) in w.arith_bad:
            return False
        for awb, lines in shipments(cp["f"].get("lines") or []).items():
            if awb and w.pickup(awb, lines[0], cp["ref"], c)[1] not in ("tracking", "email"):
                return False
    return True


def action(it: dict, mode: str, target_total: Decimal, open_lanes: set) -> str:
    v, cls = it["verdict"], it["class"]
    if v == "no_finding":
        return "none"
    if v == "conflict":
        return "escalate"
    if cls == "expiry":
        return "internal:alert"
    if cls == "late_fee_risk":
        return "internal:payment_reminder"
    if cls == "cashback" and not it.get("_outbound"):
        return "internal:track_credit"
    remedy = "request_document" if v == "cannot_determine" else ("claim_credit" if cls == "cashback" else "dispute")
    if v == "finding" and target_total >= REVIEW_AT:
        return "human_review"
    if mode != "autonomous":
        return "draft:" + remedy
    safe = bool(it.get("_safe")) and not (set(it.get("_lanes") or ()) & open_lanes) and target_total < REVIEW_AT \
        and (v != "cannot_determine" or bool(((it.get("missing_doc") or {}).get("holder") or {}).get("person")))
    return ("execute:" + remedy) if safe else "human_review"


def confidence(it: dict) -> float:
    """Our probability that the verdict and the amount are both right, from the weak spots we can see."""
    v = it["verdict"]
    p = {"finding": 0.93, "conflict": 0.85, "cannot_determine": 0.85}.get(v, 0.9)
    if it["class"] == "cashback":
        p -= 0.08                           # a sum over many invoices: one misread line moves it
    if it["class"] == "late_fee_risk":
        p -= 0.03
    pickups = it.get("_pickup") or set()
    if pickups - {"tracking", "email"}:
        p -= 0.12                           # priced on a date that isn't from the carrier's tracking
    if it.get("_arith"):
        p -= 0.25                           # the invoice's own arithmetic fails: we misread it, or it is wrong
    if it.get("_learned"):
        p -= 0.02
    if str(it.get("_currency") or "USD").upper().startswith("INR"):
        p -= 0.03
    if not it["evidence"]:
        p -= 0.3
    return round(max(0.05, min(0.99, p)), 2)


def public(it: dict) -> dict:
    return {k: (sorted(v) if isinstance(v, set) else v) for k, v in it.items() if not k.startswith("_")}


def disputed_by_invoice(items: list[dict]) -> dict[str, Decimal]:
    """What we dispute on each invoice, in its own currency: it never enters a late-fee base."""
    out: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for it in items:
        if it["verdict"] == "finding" and it.get("invoice_id") and it.get("_outbound") and not it.get("_resend"):
            out[it["invoice_id"]] += it.get("_native") or Decimal(it["amount_usd"])
    return out


# ---- the report -------------------------------------------------------------------------------------------------------
def build(state: Path, as_of: date, mode: str, feedback: list[dict], prev: dict) -> dict:
    w = World(state, as_of, feedback)
    w.arith_bad = {k for k, (c, _) in invoice_copies(w).items() if arith_problems(c["f"])}
    items: list[dict] = []
    for fn in (overcharges, duplicates, waived):
        fn(w, items)
    items = apply_decisions(w, items, drop=("reject",))     # a rejected key is not disputed; a closed one stays settled
    late_fees(w, items, disputed_by_invoice(items))
    late_fee_risk(w, items, disputed_by_invoice(apply_decisions(w, items, drop=("reject",))))
    cashback(w, items)
    margin_step_down(w, items)
    expiry(w, items)
    items = apply_decisions(w, items)
    for it in items:                         # an invoice whose own numbers don't add up is a weaker basis
        if it.get("invoice_id") in w.arith_bad:
            it["_arith"] = True
    items = finish(w, items, mode, prev)
    findings = [public(i) for i in items if i["class"] in INVOICE_CLASSES]
    pf = {"cashback": [public(i) for i in items if i["class"] == "cashback"],
          "expiry": [public(i) for i in items if i["class"] == "expiry"],
          "late_fee_risk": [public(i) for i in items if i["class"] == "late_fee_risk"]}
    return {"schema_version": "1.0", "as_of": as_of.isoformat(), "mode": mode, "findings": findings,
            "portfolio": pf, "diagnostics": {"invoice_ids": sorted(w.invoices),
                "invoice_line_counts": {k: len(cp["f"].get("lines") or []) for k, (cp, _) in invoice_copies(w).items()},
                "invoice_totals": {k: money(D(cp["f"].get("total"))) if D(cp["f"].get("total")) is not None else None
                                   for k, (cp, _) in invoice_copies(w).items()},
                "credit_note_ids": sorted(str(cn.get("number")) for cn in w.credit_notes if cn.get("number")),
                "unreadable_documents": w.unreadable}}
