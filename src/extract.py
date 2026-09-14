"""Extract grounded header fields, totals, and line candidates from OCR text.

Every extracted header value carries its page and a nearby snippet for
grounding and review.

Regexes keep extraction offline and explainable; a missed pattern yields an
honest blank rather than a guess.
"""
from __future__ import annotations

import re
from datetime import datetime


_CURRENCY = set("€$£¥₹ \t")


def normalize_number(raw) -> str:
    """Normalize a printed number to the dot-decimal format required downstream."""
    if raw is None:
        return ""
    if isinstance(raw, (int, float)):
        return str(raw)
    value = str(raw).strip().replace("%", "").strip()
    index = 0
    while index < len(value) and value[index] in _CURRENCY:
        index += 1
    value = value[index:].strip()
    value = re.sub(r"\s*[A-Za-z€$£¥₹].*$", "", value).strip()
    value = re.sub(r"[^0-9.,\-\s]", "", value).strip()
    if value.endswith(",") and "." in value:
        value = value[:-1]
    if not value:
        return ""
    last_dot = value.rfind(".")
    last_comma = value.rfind(",")
    if last_dot != -1 and last_comma != -1:
        if last_comma > last_dot:
            value = value.replace(".", "").replace(" ", "").replace(",", ".")
        else:
            value = value.replace(",", "").replace(" ", "")
    elif last_comma != -1:
        tail = re.sub(r"\s", "", value[last_comma + 1:])
        if len(tail) == 2:
            value = value.replace(" ", "").replace(",", ".")
        elif len(tail) == 3 and value.count(",") == 1:
            value = value.replace(",", "").replace(" ", "")
        else:
            value = value.replace(" ", "").replace(",", ".")
    else:
        value = value.replace(" ", "").replace(",", "")
    try:
        float(value)
    except ValueError:
        return ""
    return value

# Invoice Number
# Value is always the LAST group (earlier groups are the label words
# themselves, e.g. "Number" in "Invoice Number ..."). Patterns stay tight:
# "Arve kokku -400" is a TOTAL, not a number, so arve only counts with nr/no.
_INV_PATTERNS = [
    r"rechnung\s*nr\.?\s*:?\s*([A-Za-z0-9\-/]+)",
    r"invoice\s*(?:number|no\.?|#)\s*:?\s*([A-Za-z0-9\-/]+)",
    r"\bFAC\s+([A-Z0-9/\-]+)",
    r"\bfatura\s*(?:no\.?)?\s*:?\s*([A-Za-z0-9\-/]+)",
    r"\barve\s*(?:nr|no)\s*:?\s*(\d[\w\-/]*)",
    r"credit\s*note\s*no\.?\s*:?\s*([A-Za-z0-9\-/]+)",
    r"\binvoice\s+(\d{4,})",  # bare "Invoice 20423591" (no number/no/#)
]

# Dates: label -> ISO ---
_DATE_LABELS = [
    ("invoice_date", r"(rechnungsdatum|invoice date|data de emiss[aã]o|kuup[aä]ev|issue date)\s*:?\s*"),
    ("due_date", r"(due date|f[aä]llig|vencimento|makset[aä]htaeg|zahlbar bis)\s*:?\s*"),
]
_DATE_RES = [
    (r"(\d{1,2})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{4})", "dmy"),
    (r"(\d{4})\s*[.\-/]\s*(\d{2})\s*[.\-/]\s*(\d{2})", "ymd"),
    (r"(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{4})", "mdy"),  # 01 May 2026
]

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

# Totals. Earlier entries are more specific and therefore safer than generic
# "total", which also occurs on tax, duty, and line-summary rows.
_GROSS_LABELS = [
    "total tax inclusive", "total inclusive", "total da factura", "total a pagar",
    "total payment", "total payable", "amount payable", "endbetrag", "invoice total",
    "amount due", "grand total", "total amount", "arve kokku", "kogusumma",
    "please pay", "kokku", "tasuda", "total eur", "total gbp", "total :", "total",
]
_NON_GROSS_CONTEXT = ("subtotal", "sub total", "net amount", "tax amount", "total tax",
                      "total liquid", "total liquida", "duty", "levy")
_CURRENCY_CODES = ["EUR", "GBP", "USD", "CHF", "GHS", "GHC", "KES", "ZAR", "SGD", "MYR", "DKK", "SEK", "THB", "AUD", "CAD", "AED"]
_CURRENCY_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "CHF": "CHF"}
_CODE_TO_ISO = {"GHC": "GHS"}


def _snippet(text: str, pos: int, width: int = 60) -> str:
    s = max(0, pos - width)
    return " ".join(text[s:pos + width].split())


def extract_invoice_number(text: str, page: str) -> dict:
    # Layout jumble puts words between label and value ("Invoice Number
    # Parnu mnt 117 ... INV-1587"), so scan ALL matches per pattern and take
    # the first one containing a digit; label words ("Number", "Parnu") lose.
    for pat in _INV_PATTERNS:
        fallback = None
        for m in re.finditer(pat, text, re.IGNORECASE):
            groups = [g for g in m.groups() if g]
            val = groups[-1].strip() if groups else ""
            if not val:
                continue
            if any(ch.isdigit() for ch in val):
                return {"value": val, "page": page, "snippet": _snippet(text, m.start())}
            # Label jumble ("Invoice Number Parnu mnt 117 ... INV-1587"):
            # look ahead 150 chars. Prefer letter-bearing codes (INV-1587)
            # over bare address numbers (117) — addresses rarely book.
            ahead = text[m.end():m.end() + 150]
            am = re.search(r"\b([A-Z]{2,}-?\d[\w/\-]*)\b", ahead)
            if am:
                return {"value": am.group(1), "page": page, "snippet": _snippet(text, m.start())}
            if fallback is None and len(val) >= 3:
                fallback = (val, m.start())
        if fallback:
            return {"value": fallback[0], "page": page, "snippet": _snippet(text, fallback[1])}
    return {"value": "", "page": page, "snippet": ""}


def _parse_date(d: str, order: str) -> str:
    try:
        if order == "ymd":
            y, m, dd = int(d[0]), int(d[1]), int(d[2])
        elif order == "mdy":
            dd, m, y = int(d[0]), _MONTHS[d[1].lower()[:3]], int(d[2])
        else:
            dd, m, y = int(d[0]), int(d[1]), int(d[2])
        return datetime(y, m, dd).strftime("%Y-%m-%d")
    except (ValueError, KeyError):
        return ""


def extract_dates(text: str, page: str) -> dict:
    out: dict[str, dict] = {}
    for field, lab in _DATE_LABELS:
        m = re.search(lab, text, re.IGNORECASE)
        window = text[m.start():m.start() + 120] if m else text[:2000]
        found = {"value": "", "page": page, "snippet": ""}
        for pat, order in _DATE_RES:
            dm = re.search(pat, window, re.IGNORECASE)
            if dm:
                iso = _parse_date([dm.group(1), dm.group(2), dm.group(3)], order)
                if iso:
                    base = m.start() if m else dm.start()
                    found = {"value": iso, "page": page, "snippet": _snippet(text, base)}
                    break
        out[field] = found
    return out


def extract_currency(text: str, gross_line: str = "") -> dict:
    # Priority 1: code printed ON the gross-total line ("KES 70,654.30").
    # Footer bank blocks ("Euro (EUR): account...") used to win and lie.
    for src in (gross_line,):
        for code in _CURRENCY_CODES:
            if re.search(rf"\b{code}\b", src):
                iso = _CODE_TO_ISO.get(code, code)
                return {"value": iso, "snippet": _snippet(text, text.find(code))}
    t = text
    m = re.search(r"currency\s*in\s*:?\s*([A-Z]{3})", t, re.IGNORECASE)
    if m:
        code = m.group(1).upper()
        return {"value": _CODE_TO_ISO.get(code, code), "snippet": _snippet(t, m.start())}
    m = re.search(r"(invoice total|amount due|endbetrag|kogusumma|kokku|total)\s+([A-Z]{3})", t, re.IGNORECASE)
    if m and m.group(2).upper() in _CURRENCY_CODES:
        code = m.group(2).upper()
        return {"value": _CODE_TO_ISO.get(code, code), "snippet": _snippet(t, m.start())}
    for code in _CURRENCY_CODES:
        if re.search(rf"\b{code}\b", t):
            return {"value": _CODE_TO_ISO.get(code, code), "snippet": _snippet(t, t.find(code))}
    for sym, code in _CURRENCY_SYMBOLS.items():
        if sym in t:
            return {"value": code, "snippet": f"symbol {sym} seen"}
    return {"value": "", "snippet": ""}


def _numbers_on(line: str, thousands: bool = True) -> list[str]:
    # thousands=True joins French gaps ("Total 1 234,56"). Row context passes
    # False: there "6 457.24" is qty 6 + price 457.24, not 6457.24 — the two
    # shapes are identical, only context disambiguates.
    if thousands:
        line = re.sub(r"(?<![\d.,])(\d{1,3})\s+(?=\d{3}[.,])", r"\1", line)

    out = []
    for raw in re.findall(r"-?\d[\d.,]*", line):
        norm = normalize_number(raw)
        if norm:
            try:
                float(norm)
                out.append(norm)
            except ValueError:
                continue
            
    return out


def _amount_after(text: str, pos: int, prefer_last: bool = False) -> str:
    # Amounts sit at the END of the label's own line ("Kokku: 479,27").
    # Some layouts put it on the NEXT line ("PLEASE PAY\nKES 70,654.30"),
    # so fall through to the next 2 lines before giving up.
    lines = text[pos:].split("\n")[:6]
    amounts = []
    for ln in lines:
        nums = _numbers_on(ln)
        if nums:
            amounts.append(nums[-1])
    if not amounts:
        return ""
    return amounts[-1] if prefer_last else amounts[0]


def extract_totals(text: str, page: str) -> dict:
    # A conversion section is a second currency view, not a second payable.
    primary_text = re.split(r"conversion to .*? for tax purpose only", text,
                            maxsplit=1, flags=re.IGNORECASE)[0]
    primary_low = primary_text.lower()
    candidates: list[tuple[int, int, str, str, str]] = []
    for priority, lab in enumerate(_GROSS_LABELS):
        start = 0
        while True:
            i = primary_low.find(lab, start)
            if i == -1:
                break
            line_start = primary_text.rfind("\n", 0, i) + 1
            line_end = primary_text.find("\n", i)
            if line_end == -1:
                line_end = len(primary_text)
            line = primary_text[line_start:line_end]
            if lab in {"total", "total :", "kokku", "tasuda"} and any(
                    marker in line.lower() for marker in _NON_GROSS_CONTEXT):
                start = i + len(lab)
                continue
            prefer_last = lab in {"total payment", "total a pagar", "amount payable", "please pay"}
            amt = _amount_after(primary_text, i + len(lab), prefer_last=prefer_last)
            if amt:
                window = " ".join(primary_text[i:].split("\n")[:3])
                candidates.append((priority, i, amt, window, line.strip()))
            start = i + len(lab)
    # Some customs and multilingual invoices print the final amount as words
    # rather than beside a reliable total label. Prefer a final explicitly
    # currency-marked amount over an earlier tax or duty subtotal.
    tail_lines = primary_text.splitlines()[-20:]
    for tail_line in reversed(tail_lines):
        if not re.search(r"\b(?:euro?s?|eur)\b", tail_line, re.IGNORECASE):
            continue
        tail_numbers = _numbers_on(tail_line)
        if tail_numbers:
            return {"gross_total": {"value": tail_numbers[-1], "page": page,
                                     "snippet": tail_line.strip()[:120]},
                    "gross_line": tail_line.strip()}
    if candidates:
        _, i, amt, line, _ = max(candidates, key=lambda candidate: (-candidate[0], candidate[1]))
        return {"gross_total": {"value": amt, "page": page, "snippet": _snippet(text, i)},
                "gross_line": line}
    return {"gross_total": {"value": "", "page": page, "snippet": ""}, "gross_line": ""}


def extract_parties(text: str, page: str) -> dict:
    emails = re.findall(r"[\w.\-]+@[\w.\-]+\.\w+", text)
    vat_ids = re.findall(
        r"\b(?:GHA-VAT-[0-9]+|[A-Z]{2}[0-9][0-9A-Z.\-]{5,})\b",
        text,
        re.IGNORECASE,
    )
    # Supplier name: first line with a company suffix that is not the buyer.
    supplier = ""
    for line in text.splitlines()[:15]:
        if re.search(r"(GmbH|Ltd|Limited|OU|OÜ|Lda|PLC|Sdn Bhd|CC|AG|Inc)", line, re.IGNORECASE):
            if not re.search(r"(northwind|bolt)", line, re.IGNORECASE):
                supplier = line.strip()[:100]
                break
    return {
        "supplier_name": {"value": supplier, "page": page, "snippet": supplier},
        "emails": emails[:5],
        "vat_ids": vat_ids[:5],
    }


def extract_line_candidates(text: str, page: str) -> list[dict]:
    """Raw table-row candidates: lines ending in qty/price/amount numbers.

    Day-2 scope: return the raw row + any trailing number triple. Day 3
    decides net-vs-gross and builds schema line_items from these.
    """
    rows = []
    for line in text.splitlines():
        nums = re.findall(r"-?\d[\d\s.,]*", line)
        if len(nums) >= 3 and len(line.strip()) > 20:
            rows.append({
                "raw": line.strip()[:200],
                "trailing": [normalize_number(n) for n in nums[-3:]],
                "page": page,
            })
    return rows[:35]


def extract_file(page_texts: list[tuple[str, str]]) -> dict:
    """page_texts = [(page_id, text)]. Returns grounded header extraction."""
    if not page_texts:
        blank = {"value": "", "page": "", "snippet": ""}
        return {
            "invoice_number": blank.copy(),
            "dates": {"invoice_date": blank.copy(), "due_date": blank.copy()},
            "currency": {"value": "", "snippet": ""},
            "totals": {"gross_total": blank.copy(), "gross_line": ""},
            "parties": {"supplier_name": blank.copy(), "emails": [], "vat_ids": []},
            "line_candidates": [],
        }
    full = "\n".join(t for _, t in page_texts)
    first_page, _ = page_texts[0]
    inv = extract_invoice_number(full, first_page)
    dates = extract_dates(full, first_page)
    totals = extract_totals(full, first_page)
    cur = extract_currency(full, totals.get("gross_line", ""))
    parties = extract_parties(full, first_page)
    lines = []
    for pid, t in page_texts:
        lines.extend(extract_line_candidates(t, pid))
    return {
        "invoice_number": inv,
        "dates": dates,
        "currency": cur,
        "totals": totals,
        "parties": parties,
        "line_candidates": lines,
    }