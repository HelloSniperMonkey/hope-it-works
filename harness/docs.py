"""Read the mailbox the way the grader does, so every quote we cite is on the page we say.

The text functions are a copy of grader/extract.py (the grader keeps its reader standalone, so we keep ours): text PDFs
are read line by line in reading order, XLSX sheets row by row with cells joined by " | ", an email as its headers and
plain-text body. A PDF with no text layer is a scan: its pages are rendered to PNG for the vision model.
"""
from __future__ import annotations

import hashlib
import io
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

SEP = " | "


def norm(s: str) -> str:
    return " ".join(s.split())


# ---- PDF (same as grader/extract.py) ----------------------------------------------------------------------------------
def _reading_order(page) -> str:
    words = page.get_text("words")
    words.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines: list[list] = []
    for w in words:
        mid = (w[1] + w[3]) / 2
        if lines and abs(lines[-1][0] - mid) <= 2.5:
            lines[-1][1].append(w)
        else:
            lines.append([mid, [w]])
    return "\n".join(" ".join(x[4] for x in sorted(ws, key=lambda w: w[0])) for _, ws in lines)


def pdf_pages(data: bytes) -> list[str]:
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return [_reading_order(p) for p in doc]


def pdf_is_image_only(data: bytes) -> bool:
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return all(not p.get_text().strip() for p in doc)


def pdf_page_pngs(data: bytes, dpi: int = 200) -> list[bytes]:
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return [p.get_pixmap(dpi=dpi).tobytes("png") for p in doc]


# ---- XLSX (same as grader/extract.py) ---------------------------------------------------------------------------------
def _cell_text(cell) -> str:
    v = cell.value
    if v is None:
        return ""
    fmt = cell.number_format or "General"
    prefix = ""
    m = re.match(r'^"([^"]*)"(.*)$', fmt)
    if m:
        prefix, fmt = m.group(1), m.group(2)
    if isinstance(v, datetime):
        return prefix + v.date().isoformat()
    if isinstance(v, date):
        return prefix + v.isoformat()
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        dp = len(fmt.split(".")[1]) if "." in fmt else 0
        return prefix + f"{Decimal(str(v)):.{dp}f}"
    return prefix + str(v)


def xlsx_pages(data: bytes) -> list[str]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    pages = []
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows():
            cells = [t for t in (_cell_text(c) for c in row) if t != ""]
            if cells:
                rows.append(SEP.join(cells))
        pages.append("\n".join(rows))
    return pages


# ---- email (same as grader/extract.py) --------------------------------------------------------------------------------
def parse_eml(data: bytes):
    return BytesParser(policy=policy.default).parsebytes(data)


def email_lines(msg) -> list[str]:
    name, addr = parseaddr(str(msg["From"]))
    out = [f"From: {name} <{addr}>", f"To: {msg['To']}"]
    if msg["Cc"]:
        out.append(f"Cc: {msg['Cc']}")
    dt = parsedate_to_datetime(str(msg["Date"]))
    out += [f"Date: {dt.strftime('%Y-%m-%dT%H:%M:%SZ')}", f"Subject: {msg['Subject']}", ""]
    body = msg.get_body(preferencelist=("plain",))
    out += body.get_content().replace("\r\n", "\n").rstrip("\n").split("\n") if body else []
    names = [p.get_filename() for p in msg.iter_attachments()]
    if names:
        out.append("Attachments: " + ", ".join(names))
    return out


# ---- one mailbox document ---------------------------------------------------------------------------------------------
class Doc:
    """One citable document: an email (one page) or one attachment of it. `ref` is the grader's evidence path."""

    def __init__(self, ref: str, ext: str, pages: list[str], meta: dict, scan_pngs: list[bytes] | None = None):
        self.ref, self.ext, self.pages, self.meta, self.scan_pngs = ref, ext, pages, meta, scan_pngs
        # content only, so the same attachment sent twice (or in another mailbox) shares one extraction
        self.sha = hashlib.sha256((ext + "\0" + "\f".join(pages)).encode()
                                  + b"".join(scan_pngs or [])).hexdigest()[:24]

    @property
    def is_scan(self) -> bool:
        return self.scan_pngs is not None


def read_email(path: Path, batch: str) -> list[Doc]:
    """-> the email itself, then each attachment, as Docs sharing the email's metadata (sender, received date)."""
    data = path.read_bytes()
    msg = parse_eml(data)
    name, addr = parseaddr(str(msg["From"]))
    received = parsedate_to_datetime(str(msg["Date"])).astimezone(timezone.utc)
    ref = f"{batch}/{path.name}"
    atts = [(p.get_filename() or "", p.get_content()) for p in msg.iter_attachments()]
    meta = {"email": ref, "batch": batch, "from_name": name, "from_addr": addr.lower(), "to": str(msg["To"] or ""),
            "cc": str(msg["Cc"] or ""), "subject": str(msg["Subject"] or ""),
            "received": received.strftime("%Y-%m-%dT%H:%M:%SZ"), "attachments": [n for n, _ in atts]}
    docs = [Doc(ref, "eml", ["\n".join(email_lines(msg))], meta)]
    for fname, blob in atts:
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
        if ext == "pdf":
            if pdf_is_image_only(blob):
                pngs = pdf_page_pngs(blob)
                docs.append(Doc(f"{ref}#{fname}", ext, [""] * len(pngs), meta, scan_pngs=pngs))
            else:
                docs.append(Doc(f"{ref}#{fname}", ext, pdf_pages(blob), meta))
        elif ext == "xlsx":
            docs.append(Doc(f"{ref}#{fname}", ext, xlsx_pages(blob), meta))
    return docs


def numbered(pages: list[str]) -> str:
    """Page text with a [page:line] tag on every line, so the model can point at lines instead of copying them."""
    out = []
    for p, text in enumerate(pages, 1):
        out.append(f"=== page {p} ===")
        out += [f"[{p}:{i}] {line}" for i, line in enumerate(text.split("\n"), 1)]
    return "\n".join(out)


_REF = re.compile(r"^\s*(\d+)\s*:\s*(\d+)\s*(?:-\s*(?:(\d+)\s*:\s*)?(\d+))?\s*$")


def quote_of(pages: list[str], src: str) -> tuple[int, str] | None:
    """'3:12' or '3:12-14' (or '3:12-4:2') -> (page, exact text of those lines on that page). A range that crosses a
    page break keeps the first page's part, since a quote has to sit on one page."""
    m = _REF.match(str(src or ""))
    if not m:
        return None
    p, a = int(m.group(1)), int(m.group(2))
    p2 = int(m.group(3)) if m.group(3) else p
    b = int(m.group(4)) if m.group(4) else a
    if not 1 <= p <= len(pages):
        return None
    lines = pages[p - 1].split("\n")
    if not 1 <= a <= len(lines):
        return None
    end = b if p2 == p else len(lines)
    text = " ".join(x.strip() for x in lines[a - 1:max(a, min(end, len(lines)))])
    return (p, text) if text.strip() else None
