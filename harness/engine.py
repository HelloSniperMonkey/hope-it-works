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
    if x is None or isinstance(x, bool):
        return None
    s = re.sub(r"[,\s%$]|USD|INR|kg", "", str(x), flags=re.I)
    try:
        return Decimal(s) if s else None
    except InvalidOperation:
        return None


def day(x) -> date | None:
    try:
        return date.fromisoformat(str(x)[:10])
    except (TypeError, ValueError):
        return None


def money(x) -> str:
    return f"{rhu(x):.2f}"


def norm_inv(s) -> str:
    return re.sub(r"[\s#:]", "", str(s or "")).upper()


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
    best = None
    for alias, code in codes.items():
        if alias and (lab.startswith(alias + " ") or lab == alias) and (best is None or len(alias) > len(best[0])):
            best = (alias, code)
    return best[1] if best else None


class World:
    """Everything the mailbox has said by as_of (end of day UTC), indexed for the rules."""

    def __init__(self, state: Path, as_of: date, feedback: list[dict]):
        self.as_of, self.feedback = as_of, feedback
        self.contracts: dict[str, Contract] = {}
        self.instruments, self.notices, self.waivers, self.extensions = [], [], [], []
        self.events: dict[str, list] = defaultdict(list)      # awb -> [(event, date, source, row/ev, ref)]
        self.invoices: dict[str, list] = defaultdict(list)    # invoice id -> copies, in order received
        self.credit_notes, self.registers, self.refs, self.email_fx = [], [], [], []
        self.awbs: dict[str, tuple] = {}
        self.dgds: dict[str, tuple] = {}
        self.automation: list[dict] = []
        self.docs_received: dict[str, date] = {}
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
                self._add(d["kind"], d.get("facts") or {}, d["ref"], meta, rec_day, email_facts, d.get("pages"))

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
                if ev.get("awb") and day(ev.get("date")):
                    self.events[ev["awb"].strip().upper()].append(
                        {"event": ev["event"], "date": day(ev["date"]), "source": "email", "fault": ev.get("fault"),
                         "reason": ev.get("reason_code"), "obj": ev, "ref": ref, "meta": meta})
            for x in f.get("fx_rates") or []:
                self.email_fx.append({**x, **ctx})
        elif kind == "contract" and f.get("contract_id"):
            c = Contract(f, ref, rec)
            self.contracts.setdefault(c.id, c)
        elif kind == "contract_instrument":
            self.instruments.append({**f, **ctx})
        elif kind == "invoice" and f.get("invoice_number"):
            self.invoices[norm_inv(f["invoice_number"])].append({**ctx, "f": f, "email": email_facts})
        elif kind == "credit_note":
            self.credit_notes.append({**f, **ctx})
        elif kind == "air_waybill" and f.get("awb"):
            self.awbs.setdefault(f["awb"].strip().upper(), (f, ref))
        elif kind == "dg_declaration" and f.get("awb"):
            self.dgds.setdefault(f["awb"].strip().upper(), (f, ref))
        elif kind == "tracking_export":
            for row in f.get("rows") or []:
                if row.get("awb") and day(row.get("date")):
                    self.events[row["awb"].strip().upper()].append(
                        {"event": row["event"], "date": day(row["date"]), "source": "tracking", "fault": None,
                         "reason": row.get("reason_code"), "obj": row, "ref": ref, "meta": meta})
        elif kind == "ap_register":
            self.registers.append({**f, **ctx})

    # -- people and authority ------------------------------------------------------------------------------------------
    def authorised(self, c: Contract | None, category: str, email: str | None, name: str | None, on: date | None,
                   party: str | None = None):
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
        for fb in self.feedback:
            fact = fb.get("fact") or {}
            if fb.get("decision") != "confirm" or fact.get("type") != "sender_authority":
                continue
            sc = (fb.get("scope") or {}).get("applies_to") or {}
            cats = (fact.get("value") or {}).get("categories") or []
            who = sc.get("person") or (fact.get("subject") or {}).get("person")
            if sc.get("contract_id") not in (None, c.id) or (sc.get("category") and sc["category"] != category) \
                    or category not in cats or norm_label(who) != norm_label(name):
                continue
            if in_scope(fb, on):
                return True, None, fb
        return False, None, None

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

    def due_date(self, inv_id: str, c: Contract | None, base_due: date | None):
        """Due date after any extension from the carrier's payment-terms person -> (date, evidence, feedback)."""
        due, ev, fb_used = base_due, [], None
        for x in self.extensions:
            if norm_inv(x.get("invoice_number")) != inv_id or yes(x.get("quoted_only")):
                continue
            email = x["meta"]["from_addr"] if not x.get("written_by_email") else x["written_by_email"]
            ok, sch, fb = self.authorised(c, "payment_terms", email, x.get("written_by_name") or
                                          x["meta"]["from_name"], x["received"], c.carrier if c else None)
            nd = day(x.get("new_due_date"))
            if ok and nd:
                due, ev, fb_used = nd, cites(cite(x["ref"], x), sch), fb
        return due, ev, fb_used

    def pickup(self, awb: str | None, line: dict | None, inv_ref: str | None, c: Contract | None):
        """-> (date, source, evidence): tracking export, then a carrier "picked up" email, then the AWB, then the
        date printed on the invoice line."""
        if awb:
            evs = self.events.get(awb.strip().upper(), [])
            for src in ("tracking", "email"):
                hits = [e for e in evs if e["event"] == "PICKED_UP" and e["source"] == src
                        and (src == "tracking" or self.from_carrier(e["meta"], c))]
                if hits:
                    e = hits[0]
                    return e["date"], src, cites(cite(e["ref"], e["obj"]))
            if awb.strip().upper() in self.awbs:
                f, ref = self.awbs[awb.strip().upper()]
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
        return sorted(out, key=lambda i: (day(i.get("effective_from")) or date.min, i["received"]))

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
    return on is None or ((a is None or a <= on) and (b is None or on <= b))


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

    def __init__(self, w: World, c: Contract):
        self.w, self.c = w, c

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
                                               r["received"])
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
                                       n["received"], c.carrier)
            if not ok and not accepted:
                continue
            if any(cite(n["ref"], x) in w.automation for x in [n]):
                continue
            notice = {"label": f"rate notice {n.get('reference') or ''}".strip(), "rows": n.get("rows") or [],
                      "ref": n["ref"], "level": 2, "eff": a, "kind": "notice", "received": n["received"],
                      "obj": n, "sch": sch}
            notice_fb = accepted or fb
        if missing and (not amd or (missing.get("eff") or date.min) >= (amd["eff"] or date.min)):
            return {"kind": "missing", "missing": missing, "msa": msa}
        if amd and notice:
            prec, fb = self.precedence(lane, pickup)
            if fb and not (amd["eff"] and day((fb.get("scope") or {}).get("valid_from"))
                           and amd["eff"] > day(fb["scope"]["valid_from"])):
                use = fb.get("fact", {}).get("value", {}).get("use", "")
                return {"kind": "ok", "src": notice if "notice" in use else amd, "msa": msa, "fb": fb}
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
        for fb in self.w.feedback:
            fact = fb.get("fact") or {}
            if fact.get("type") != "rate_accepted":
                continue
            subj, val = fact.get("subject") or {}, fact.get("value") or {}
            sc = (fb.get("scope") or {}).get("applies_to") or {}
            if str(sc.get("contract_id") or subj.get("contract_id") or "").upper() != self.c.id.upper() or \
                    norm_lane(sc.get("lane") or subj.get("lane")) != lane or not in_scope(fb, pickup):
                continue
            if val.get("sent_by") and norm_label(val["sent_by"]) != norm_label(n.get("written_by_name") or
                                                                               n["meta"]["from_name"]):
                continue
            if val.get("sent_on") and day(val["sent_on"]) != n["received"]:
                continue
            return fb
        return None

    def precedence(self, lane: str, pickup: date):
        for fb in self.w.feedback:
            fact = fb.get("fact") or {}
            if fact.get("type") != "precedence":
                continue
            sc = (fb.get("scope") or {}).get("applies_to") or {}
            subj = fact.get("subject") or {}
            if str(sc.get("contract_id") or subj.get("contract_id") or "").upper() == self.c.id.upper() and \
                    norm_lane(sc.get("lane") or subj.get("lane")) == lane and in_scope(fb, pickup):
                return fact.get("value"), fb
        return None, None

    def freight(self, src: dict, cw: Decimal):
        """-> (amount, evidence) for base freight under one source, minimum charge included."""
        row = band_of(src["rows"], cw)
        if row is None or D(row.get("rate")) is None:
            return None, []
        amt = rhu(D(row["rate"]) * cw)
        ev = [cite(src["ref"], row)]
        mrow = min_of(src["rows"]) or (min_of(src.get("msa_rows") or []) if src.get("msa_rows") else None)
        if mrow and D(mrow.get("rate")) is not None and amt < D(mrow["rate"]):
            amt = rhu(D(mrow["rate"]))
            ev.append(cite(src["ref"], mrow))
        return amt, cites(*ev)


# ---- the detectors ----------------------------------------------------------------------------------------------------
def shipments(lines: list[dict]) -> dict:
    out = defaultdict(list)
    for ln in lines:
        out[(ln.get("awb") or "").strip().upper()].append(ln)
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
        pr = Pricer(w, c)
        inv_date = day(f.get("invoice_date"))
        diffs, ev, flags = [], [], {"pickup": set(), "lanes": set(), "levels": True, "issues": 0, "learned": None,
                                    "retro": False}
        conflict = None
        missing = None
        for awb, lines in shipments(f.get("lines") or []).items():
            if not awb:
                continue
            first = lines[0]
            pickup, psrc, pev = w.pickup(awb, first, ref, c)
            if pickup is None:
                continue
            lane = norm_lane(first.get("lane"))
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
                        a, e = pr.freight({**s, "msa_rows": (res["msa"] or {}).get("rows")}, cw)
                        amts.append((a, e, s))
                    if any(a is None for a, _, _ in amts):
                        continue
                    if amts[0][0] != amts[1][0]:
                        conflict = {"amts": amts, "line": ln, "clause": res.get("clause"), "pickup": pickup,
                                    "pev": pev, "lane": lane}
                        correct_frt[id(ln)] = None
                        flags["lanes"].add((c.id, lane, c.period_of(pickup)))
                        continue
                    res = {"kind": "ok", "src": res["a"], "msa": res["msa"]}   # they agree: nothing to decide
                src = res["src"]
                amt, e = pr.freight({**src, "msa_rows": (res["msa"] or {}).get("rows")}, cw)
                if amt is None:
                    continue
                correct_frt[id(ln)] = amt
                billed = D(ln.get("amount"))
                if res.get("fb"):
                    flags["learned"] = res["fb"]
                if src["level"] > 2:
                    flags["levels"] = False
                flags["lanes"].add((c.id, lane, c.period_of(pickup)))
                if billed is not None and billed != amt:
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
            for ln, code in coded:
                billed = D(ln.get("amount"))
                if billed is None or code in (None, FREIGHT) or code in NOT_PRICED:
                    continue
                correct, e = line_correct(w, pr, c, code, ln, awb, pickup, base_frt, lines)
                if correct is None:
                    continue
                if billed > correct:
                    diffs.append(billed - correct)
                    ev += [cite(ref, ln)] + e
                    if code == FUEL:
                        flags["pickup"].add(psrc)
                        ev += pev
        if missing:
            out.append(item("overcharge", {"invoice_id": inv_id}, "cannot_determine",
                            evidence=[missing.pop("_inv")] + [missing.pop("_cite")],
                            missing={k: v for k, v in missing.items() if not k.startswith("_") and k != "eff"},
                            contract=c, outbound=True, safe=True, lanes=flags["lanes"]))
            continue
        pos = sum((d for d in diffs if d > 0), ZERO)
        if conflict:
            ln = conflict["line"]
            billed_f = D(ln.get("amount"))
            fuel_lines = [x for x in shipments(f.get("lines") or [])[(ln.get("awb") or "").strip().upper()]
                          if c.code_of(x.get("description")) == FUEL]
            pct, fev = pr.fuel_pct(conflict["pickup"])
            billed_total = billed_f + sum((D(x.get("amount")) or ZERO for x in fuel_lines), ZERO)
            totals = []
            for a, e, s in conflict["amts"]:
                fuel = rhu(pct / 100 * a) if (pct is not None and fuel_lines) else ZERO
                totals.append(a + fuel)
            lo, hi = max(ZERO, billed_total - max(totals)), billed_total - min(totals)
            if hi + pos > 0:
                (a1, e1, s1), (a2, e2, s2) = conflict["amts"]
                ev2 = [cite(ref, ln)] + e1 + e2 + [conflict["clause"]] + conflict["pev"]
                out.append(item("overcharge", {"invoice_id": inv_id}, "conflict", rng=(lo + pos, hi + pos),
                                evidence=ev2, contract=c, outbound=False, lanes=flags["lanes"],
                                learned=flags["learned"]))
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
        base = base_frt
        if pct is None or base is None:
            return None, []
        return rhu(pct / 100 * base), ev + cites(cite(c.ref, c.clause("fuel_rule")))
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
    if unit in ("per_kg", "per_day", "per_month"):
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
            if code in NOT_PRICED or ln.get("late_fee"):
                continue
            ident = (ln.get("awb") or "").strip().upper() or (f"period:{ln.get('period')}" if ln.get("period") else "")
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
            first = hits[0]
            dup_lines[h[2]].append((h, first))
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
        pr = Pricer(w, c) if c else None
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
            if not asks or (paid and paid > again["received"]):
                continue
            f = again["f"]
            c = w.contract_for(f.get("contract_id"))
            total = D(f.get("total"))
            row, rref = w.register_row(inv_id, lambda r: r.get("status") in ("paid", "scheduled")) or (row, rref)
            if total is None:
                continue
            usd, fxev = to_usd(Pricer(w, c), f, total) if c else (rhu(total), [])
            if usd is None:
                out.append(missing_fx(Pricer(w, c), inv_id, "duplicate", again["ref"], f))
                break
            out.append(item("duplicate", {"invoice_id": inv_id}, "finding", amount=usd,
                            evidence=[cite(rref, row), email_line(w, again, inv_id),
                                      cite(again["ref"], f, "total_src"),
                                      cite(copy["ref"], copy["f"], "number_src")] + fxev,
                            contract=c, outbound=True, safe=True, currency=f.get("currency"), native=total))
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
    """Credit notes that reverse what was billed twice: credited in full leaves nothing, in part reduces it."""
    total = ZERO
    awbs = {(ln.get("awb") or "").upper() for ln in lines}
    for cn in w.credit_notes:
        refs = {norm_inv(x) for x in cn.get("invoice_numbers") or []}
        if refs & set(inv_ids) or any((x.get("awb") or "").upper() in awbs for x in cn.get("lines") or []):
            if cn.get("credit_type") in ("dispute", "other", None):
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
            awb = (ln.get("awb") or "").strip().upper()
            when = day(ln.get("date")) or day(f.get("invoice_date"))
            for wv in w.waivers:
                if not wv_covers(c, wv, code, awb, ln, when):
                    continue
                st = per_inv.setdefault(inv_id, {"amt": ZERO, "ev": [], "f": f, "ref": ref, "c": c, "verdicts": set(),
                                                 "missing": None, "learned": None})
                email = wv.get("written_by_email") or (None if yes(wv.get("quoted_only")) else wv["meta"]["from_addr"])
                name = wv.get("written_by_name") or wv["meta"]["from_name"]
                one_off = one_off_for(w, inv_id, name, ln.get("period") or (month_of(when) if when else None))
                ok, sch, fb = w.authorised(c, "waivers", email, name, day(wv.get("written_on")) or wv["received"],
                                           c.carrier)
                if one_off:
                    ok, fb = True, one_off
                if not ok:
                    continue
                if yes(wv.get("quoted_only")) and not original_held(w, wv) and not one_off:
                    holder = c.holder("waivers") if sch else c.kam()
                    st["missing"] = {"name": f"original email from {name}" + (f" dated {wv['written_on']}" if
                                                                             wv.get("written_on") else "")
                                     + f" waiving the {wv.get('charge')}" + (f" on AWB {awb}" if awb else ""),
                                     "holder": holder}
                    st["ev"] += [cite(wv["ref"], wv), cite(ref, ln)]
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
            usd, fxev = to_usd(Pricer(w, st["c"]), st["f"], st["amt"])
            if usd is None:
                out.append(missing_fx(Pricer(w, st["c"]), inv_id, "waived_but_billed", st["ref"], st["f"]))
                continue
            out.append(item("waived_but_billed", {"invoice_id": inv_id}, "finding", amount=usd,
                            evidence=st["ev"] + fxev, contract=st["c"], outbound=True, safe=True,
                            learned=st["learned"], currency=st["f"].get("currency"), native=st["amt"]))
        elif st["missing"]:
            out.append(item("waived_but_billed", {"invoice_id": inv_id}, "cannot_determine", evidence=st["ev"],
                            missing=st["missing"], contract=st["c"], outbound=True, safe=True))


def wv_covers(c: Contract, wv: dict, code: str | None, awb: str, ln: dict, when: date | None) -> bool:
    """A waiver covers exactly the charge and the shipments or months it names (clause 11.2)."""
    if code is None or c.code_loose(wv.get("charge")) != code:
        return False
    awbs = {a.strip().upper() for a in wv.get("awbs") or []}
    months = set(wv.get("months") or [])
    if awbs:
        return awb in awbs
    if months:
        per = ln.get("period") or (month_of(when) if when else None)
        return per in months
    return False


def one_off_for(w: World, inv_id: str, person: str, month: str | None) -> dict | None:
    for fb in w.feedback:
        fact = fb.get("fact") or {}
        if fb.get("decision") == "one_off_approval" and fact.get("type") == "waiver_scope" and \
                norm_inv((fb.get("finding_key") or {}).get("invoice_id")) == inv_id and \
                norm_label((fact.get("subject") or {}).get("person")) == norm_label(person):
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
                set(map(str.upper, o.get("awbs") or [])) == set(map(str.upper, wv.get("awbs") or [])) and \
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


def late_fees(w: World, out: list[dict]):
    for inv_id, (copy, _) in invoice_copies(w).items():
        f, ref = copy["f"], copy["ref"]
        c = w.contract_for(f.get("contract_id"))
        if c is None:
            continue
        by_target: dict[str, list] = defaultdict(list)
        for ln in f.get("lines") or []:
            lf = ln.get("late_fee") or {}
            if lf.get("on_invoice") or c.code_of(ln.get("description")) == LATE:
                tgt = norm_inv(lf.get("on_invoice") or "")
                if tgt:
                    by_target[tgt].append(ln)
        total, ev, learned_fb = ZERO, [], None
        for tgt, lines in by_target.items():
            billed = sum((D(ln.get("amount")) or ZERO for ln in lines), ZERO)
            row, rref = w.register_row(tgt)
            if not row:
                continue
            paid = day(row.get("paid_date"))
            if not paid:
                continue
            due, xev, fb = w.due_date(tgt, c, day(row.get("due_date")))
            principal = D((lines[0].get("late_fee") or {}).get("overdue_amount")) or D(row.get("amount"))
            correct = fee_owed(c, principal, due, paid)
            if correct is None:
                continue
            if billed > correct:
                total += billed - correct
                ev += [cite(ref, ln) for ln in lines] + [cite(rref, row)] + xev + \
                      [cite(c.ref, c.clause("late_fee"))] + ([cite(c.ref, c.clause("extension"))] if xev else [])
                learned_fb = learned_fb or fb
        if total > 0:
            usd, fxev = to_usd(Pricer(w, c), f, total)
            if usd is None:
                out.append(missing_fx(Pricer(w, c), inv_id, "late_fee_not_owed", ref, f))
                continue
            out.append(item("late_fee_not_owed", {"invoice_id": inv_id}, "finding", amount=usd, evidence=ev + fxev,
                            contract=c, outbound=True, safe=True, learned=learned_fb, currency=f.get("currency"), native=total))


def fee_owed(c: Contract, principal: Decimal | None, due: date | None, paid: date) -> Decimal | None:
    lf = c.clause("late_fee")
    r, grace = D(lf.get("percent_per_month")), lf.get("grace_days") or 0
    per = lf.get("days_per_month") or 30
    if r is None or due is None or principal is None:
        return None
    late = (paid - due).days
    if late <= int(grace):
        return ZERO
    n = math.ceil(late / per)
    total, base = ZERO, principal
    for _ in range(n):
        fee = rhu(r / 100 * base)
        total += fee
        if lf.get("compounding"):
            base += fee
    return total


def late_fee_risk(w: World, out: list[dict], disputed: dict[str, Decimal]):
    seen = set()
    for reg in sorted(w.registers, key=lambda r: r["received"], reverse=True):
        for row in reg.get("rows") or []:
            inv_id = norm_inv(row.get("invoice_number"))
            if inv_id in seen:
                continue
            seen.add(inv_id)
            if row.get("status") != "open":
                continue
            copy = (w.invoices.get(inv_id) or [None])[0]
            f = copy["f"] if copy else {"currency": row.get("currency"), "invoice_date": row.get("invoice_date")}
            c = w.contract_for(f.get("contract_id")) or (next(iter(w.contracts.values())) if len(w.contracts) == 1
                                                         else None)
            if c is None:
                continue
            due, xev, fb = w.due_date(inv_id, c, day(row.get("due_date")))
            if due is None or not 0 < (due - w.as_of).days <= 14:
                continue
            r = D(c.clause("late_fee").get("percent_per_month"))
            principal = D(row.get("amount"))
            if r is None or principal is None:
                continue
            principal -= disputed.get(inv_id, ZERO)
            fee = rhu(r / 100 * principal)
            usd, fxev = to_usd(Pricer(w, c), {**f, "currency": row.get("currency") or f.get("currency")}, fee)
            if usd is None:
                continue
            out.append(item("late_fee_risk", {"invoice_id": inv_id}, "finding", amount=usd, deadline=due,
                            evidence=[cite(reg["ref"], row), cite(c.ref, c.clause("late_fee"))] + xev + fxev,
                            contract=c, outbound=False, learned=fb))


def cashback(w: World, out: list[dict]):
    for c in w.contracts.values():
        qs_cl = c.clause("qualifying_spend")
        rb, ic = c.clause("rebate"), c.clause("improvement_credit")
        if rb.get("applies") and rb.get("tiers"):
            for q in quarters(c, w.as_of):
                first, last = quarter_bounds(q)
                spend, ev, ok = qualifying(w, c, first, last)
                if not ok:
                    continue
                pct, tier_ev = ZERO, None
                for t in sorted(rb["tiers"], key=lambda t: D(t["min_spend"])):
                    if spend >= D(t["min_spend"]):
                        pct = D(t["percent"])
                if pct <= 0:
                    continue
                earned = rhu(pct / 100 * spend)
                due = last + timedelta(days=int(rb.get("credit_due_days") or 0))
                add_credit(w, out, c, q, "rebate", earned, due, ev + [cite(c.ref, rb), cite(c.ref, qs_cl)])
        if ic.get("applies") and D(ic.get("percent")) and c.effective:
            n = 1
            while True:
                a, b = add_years(c.effective, n - 1), add_years(c.effective, n) - timedelta(days=1)
                if b >= w.as_of or (c.end and a > c.end):
                    break
                spend, ev, ok = qualifying(w, c, a, b)
                if ok:
                    earned = rhu(D(ic["percent"]) / 100 * spend)
                    due = b + timedelta(days=1) + timedelta(days=int(ic.get("due_days_after_anniversary") or 0))
                    add_credit(w, out, c, f"CY{n}", "promised_credit", earned, due,
                               ev + [cite(c.ref, ic), cite(c.ref, qs_cl)])
                n += 1


def quarters(c: Contract, as_of: date) -> list[str]:
    if not c.effective:
        return []
    out, d = [], c.effective
    while d <= as_of:
        q = quarter_of(d)
        first, last = quarter_bounds(q)
        if last < as_of and (not c.end or first <= c.end):
            out.append(q)
        d = last + timedelta(days=1)
    return out


def qualifying(w: World, c: Contract, first: date, last: date):
    """Qualifying Spend in USD for invoices dated in [first, last], as the contract defines it."""
    cl = c.clause("qualifying_spend")
    inc = {x.upper() for x in cl.get("include_codes") or []} or {FREIGHT, FUEL, SECURITY}
    exc = {x.upper() for x in cl.get("exclude_codes") or []} | {"TAX", "DUTY", LATE}
    pr = Pricer(w, c)
    spend, ev, any_inv = ZERO, [], False
    for inv_id, (copy, _) in invoice_copies(w).items():
        f = copy["f"]
        if str(f.get("contract_id") or "").upper() != c.id.upper():
            continue
        d = day(f.get("invoice_date"))
        if not d or not first <= d <= last:
            continue
        s = ZERO
        for ln in f.get("lines") or []:
            code = c.code_of(ln.get("description"))
            if code is None or code in exc or ln.get("late_fee"):
                continue
            if code in inc or (cl.get("include_annex_d", True) and code in c.acc):
                s += D(ln.get("amount")) or ZERO
        if str(f.get("currency") or "USD").upper().startswith("INR"):
            rate, _ = pr.fx(d)
            if not rate:
                return ZERO, [], False
            s = rhu(s / rate)
        spend += s
        any_inv = True
    for cn in w.credit_notes:
        d = day(cn.get("credit_date"))
        if str(cn.get("contract_id") or "").upper() == c.id.upper() and d and first <= d <= last and \
                cn.get("credit_type") not in ("rebate", "improvement") and cl.get("net_of_credit_notes", True):
            amt = abs(D(cn.get("total")) or ZERO)
            if str(cn.get("currency") or "USD").upper().startswith("INR"):
                rate, _ = pr.fx(d)
                amt = rhu(amt / rate) if rate else ZERO
            spend -= amt
    return spend, ev, any_inv


def add_credit(w: World, out: list[dict], c: Contract, period: str, ctype: str, earned: Decimal, due: date, ev):
    kind = {"rebate": "rebate", "promised_credit": "improvement", "margin_step_down": "margin"}[ctype]
    credited, cev = ZERO, []
    for cn in w.credit_notes:
        if str(cn.get("contract_id") or "").upper() == c.id.upper() and cn.get("credit_type") == kind and \
                str(cn.get("period") or "").replace(" ", "").upper() == period.upper():
            amt = abs(D(cn.get("total")) or ZERO)
            if str(cn.get("currency") or "USD").upper().startswith("INR"):
                rate, _ = Pricer(w, c).fx(day(cn.get("credit_date")))
                amt = rhu(amt / rate) if rate else ZERO
            credited += amt
            cev.append(cite(cn["ref"], cn, "total_src"))
    owed = earned - credited
    if owed <= 0:
        return
    out.append(item("cashback", {"contract_id": c.id, "period": period, "credit_type": ctype}, "finding",
                    amount=owed, evidence=ev + cev, contract=c, outbound=w.as_of > due, safe=True, due=due))


def expiry(w: World, out: list[dict]):
    for c in w.contracts.values():
        when, ev = c.end, [cite(c.ref, c.f, "end_src")]
        if c.f.get("auto_renews"):
            nd = day(c.f.get("notice_deadline"))
            if not nd and c.end and c.f.get("renewal_notice_days"):
                nd = c.end - timedelta(days=int(c.f["renewal_notice_days"]))
            when, ev = nd or when, ev + [cite(c.ref, c.f, "renewal_src")]
        for i in w.instruments_for(c):
            if i.get("signed_by_both") and (day(i.get("new_notice_deadline")) or day(i.get("new_end_date"))):
                when = day(i.get("new_notice_deadline")) or day(i.get("new_end_date"))
                ev = [cite(i["ref"], i, "term_src")]
        if when and 0 <= (when - w.as_of).days <= 60:
            out.append(item("expiry", {"contract_id": c.id}, "finding", deadline=when, evidence=ev, contract=c,
                            outbound=False))


# ---- feedback that closes, rejects or overrides a key -----------------------------------------------------------------
def apply_decisions(w: World, items: list[dict]) -> list[dict]:
    keep = []
    for it in items:
        k = key_of(it)
        drop = False
        for fb in w.feedback:
            fk = fb.get("finding_key") or {}
            fkey = {**fk, "class": fk.get("class") or ("cashback" if "period" in fk else "expiry")}
            fkey["invoice_id"] = norm_inv(fk.get("invoice_id")) if fk.get("invoice_id") else None
            if key_of({**fkey, **({"invoice_id": fkey["invoice_id"]} if fkey.get("invoice_id") else {})}) != k:
                continue
            dec = fb.get("decision")
            if dec in ("close", "reject"):
                drop = True
            elif dec == "override_amount":
                amt = D(((fb.get("fact") or {}).get("value") or {}).get("amount_usd"))
                if amt is not None:
                    it["verdict"], it["amount_usd"], it["amount_range"] = "finding", money(amt), None
                    it["_learned"] = fb
        if not drop:
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
        it["action"] = action(it, mode, per_target[it.get("invoice_id") or key_of(it)], open_lanes)
        it["confidence"] = confidence(it)
        if it.get("_learned"):
            it["learned_at"] = learned(it["_learned"])
    for it in items:
        k = key_of(it)
        old = prev.get(k)
        base = k
        sig = (it["verdict"], it["amount_usd"], tuple(it["amount_range"] or ()))
        if old and tuple(old["sig"]) == sig:
            it["id"] = old["id"]
        elif old:
            n = int(old["id"].rsplit("#v", 1)[1]) + 1 if "#v" in old["id"] else 2
            it["id"], it["supersedes"] = f"{base}#v{n}", [old["id"]]
        else:
            it["id"] = base
            if it.get("_retro"):
                it["retro"] = True
    return items


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
    safe = bool(it.get("_safe")) and not (set(it.get("_lanes") or ()) & open_lanes) and target_total < REVIEW_AT
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


# ---- the report -------------------------------------------------------------------------------------------------------
def build(state: Path, as_of: date, mode: str, feedback: list[dict], prev: dict) -> dict:
    w = World(state, as_of, feedback)
    w.arith_bad = {k for k, (c, _) in invoice_copies(w).items() if arith_problems(c["f"])}
    items: list[dict] = []
    for fn in (overcharges, duplicates, waived, late_fees):
        fn(w, items)
    disputed: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for it in items:                         # disputed amounts leave a late-fee principal, in its own currency
        if it["verdict"] == "finding" and it.get("invoice_id") and it.get("_outbound"):
            disputed[it["invoice_id"]] += it.get("_native") or Decimal(it["amount_usd"])
    late_fee_risk(w, items, disputed)
    cashback(w, items)
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
            "portfolio": pf}

