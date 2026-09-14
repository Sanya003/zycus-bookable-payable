"""lines.py — Convert OCR table rows into structured line candidates.

Find headers/totals, join continuation lines, then parse qty, rate, unit,
total, and description. Handle multilingual headers and formats.

Never invent missing prices: description-only rows stay low-confidence.
Single amounts default to qty=1 and unit=total. Preserve raw text, page,
and confidence for review.
"""
from __future__ import annotations

import re

from .extract import _numbers_on

_HEADER_WORDS = [
    "description", "desc", "descrigao", "descri", "beschreibung", "nimetus",
    "tekst", "notes", "kood", "code", "article", "reference", "ref", "product",
    "item", "service", "leistung",
]
_QTY_WORDS = ["qty", "qtd", "quant", "menge", "kogus", "quantity", "kоличество", "tk", "arv"]
_PRICE_WORDS = ["unit price", "prego", "preço", "hind", "price", "unit", "einzelpreis", "neto", "net amount"]
_TOTAL_WORDS = ["amount", "total", "valor", "summa", "sum", "gesamtpreis", "net amount"]
_END_WORDS = [
    "subtotal", "sub total", "vahesumma", "kokku", "tasuda", "endbetrag",
    "invoice total", "grand total", "amount due", "kogusumma", "tasumata",
    "total tax", "total vat", "km kokku", "gesamtsumme", "rechnungsbetrag",
    "zahlbetrag", "brutto", "balance due", "net amount", "total amount",
    "total gbp", "total eur", "vat total", "tax total",
]
# Lines that are never items (bank footer, contacts, tax IDs).
_JUNK_PATTERNS = [
    "iban", "swift", "sort code", "account number", "bank", "bic",
    "vat reg", "vat number", "utr number", "tel", "phone", "fax",
    "customer no", "e-mail sales", "sales person", "capital social",
    "contribuinte", "lehekülg", "page no", "co.reg", "reg. no",
]
# Note/comment rows that parse as numbers but are not items.
_NOTE_WORDS = ["observacoes", "remarks", "notice", "termos", "conditions"]
_SUMMARY_WORDS = ["liquidacao", "liquida", "direitos", "anti-dumping",
                  "total tax", "tax amount", "vat amount", "incidencia",
                  "valor desc", "condicoes de pagamento"]


def _scores(line: str, words: list[str]) -> int:
    low = line.lower()
    return sum(1 for w in words if w in low)


def find_table_bounds(text_lines: list[str]) -> tuple[int, int]:
    """Return (header_idx, end_idx); -1 header means no table found."""
    header = -1
    for i, ln in enumerate(text_lines):
        if _scores(ln, _HEADER_WORDS) >= 1 and _scores(ln, _QTY_WORDS + _PRICE_WORDS) >= 1:
            header = i
            break
        if _scores(ln, _HEADER_WORDS) >= 1 and _scores(ln, _TOTAL_WORDS) >= 1:
            header = i
            break
        
    end = len(text_lines)
    start = header + 1 if header >= 0 else 0
    for j in range(start, end):
        n = len(_numbers_on(text_lines[j]))
        # Bare "total" is weak ("Total Care package" could be an item), so it
        # only ends the table with <=2 numbers; strong markers allow <=4.
        if _scores(text_lines[j], _END_WORDS) >= 1 and n <= 4:
            end = j
            break

        low = text_lines[j].lower()
        if re.search(r"\btotal\b", low) and n <= 2:
            end = j
            break
        
    return header, end


def assemble_rows(text_lines: list[str], header: int, end: int) -> list[str]:
    """Join continuation lines into row strings."""
    raw = [ln.strip() for ln in text_lines[header + 1:end] if ln.strip()]
    rows: list[str] = []
    buf = ""
    for ln in raw:
        n = len(_numbers_on(ln))
        if n >= 3:
            if buf:
                rows.append((buf + " " + ln).strip())
                buf = ""
            else:
                rows.append(ln)
        elif n == 0:
            # Attach to next numbers line if short, else keep as description-only row.
            if buf:
                rows.append(buf)
            buf = ln
        else:  # 1-2 numbers: likely a continuation fragment.
            if buf:
                rows.append((buf + " " + ln).strip())
                buf = ""
            else:
                buf = ln

    if buf:
        rows.append(buf)
    return rows


def parse_rate(row: str) -> tuple[str, str]:
    """Return (rate, rest-without-rate). 'No VAT' -> rate 0."""
    m = re.search(r"no\s*vat|non\s*taxable|exempt", row, re.IGNORECASE)
    if m:
        return "0", (row[:m.start()] + row[m.end():]).strip()
    
    # Standalone 0% only: (?<![\d.,]) stops "23,00%" matching its tail "0%".
    m = re.search(r"(?<![\d.,])0\s*%", row)
    if m:
        return "0", (row[:m.start()] + row[m.end():]).strip()
    
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", row)
    if m:
        rate = m.group(1).replace(",", ".")
        return rate, (row[:m.start()] + row[m.end():]).strip()
    
    return "", row


def _is_junk(line: str) -> bool:
    low = line.lower()
    if line.count("[") >= 2 or line.count("]") >= 2:
        return True
    if any(p in low for p in _JUNK_PATTERNS):
        return True
    
    for raw in re.findall(r"\d[\d.,]*", line):
        digits = re.sub(r"\D", "", raw)
        if len(digits) > 10:  # IBANs, account numbers, product refs
            return True
        
    return False



_QTY_UNIT_WORDS = r"Std|Stk|pcs|hours?|hrs?|units?|x|Hr|St|Stück|TK|tundi"


def _pick_qty_unit(nums: list[str], desc: str) -> tuple[str, str, str]:
    """Return (qty, unit, total). Qty evidence order: unit-word > Nx prefix
    (handled by caller) > product test (qty x unit == total)."""
    if not nums:
        return "", "", ""
    
    total = nums[-1]
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:" + _QTY_UNIT_WORDS + r")\b", desc, re.IGNORECASE)
    if m:
        qty = m.group(1).replace(",", ".")
        for cand in nums:
            if cand == m.group(1):
                continue
            try:
                if abs(float(qty) * float(cand) - float(total)) <= 0.01 + abs(float(total)) * 0.005:
                    return qty, cand, total
            except ValueError:
                continue
        # Qty certain, unit = second-to-last number.
        return qty, (nums[-2] if len(nums) >= 2 else ""), total
    
    if len(nums) >= 3:
        # Some tables include an exchange rate between unit price and total:
        # quantity, unit price, ROE, total. Search the printed numeric columns
        # for the pair that independently reconciles to the row total.
        for qty_index, qty_candidate in enumerate(nums[:-1]):
            for unit_candidate in nums[qty_index + 1:-1]:
                try:
                    if float(qty_candidate) <= 0 or float(unit_candidate) <= 0:
                        continue
                    if abs(float(qty_candidate) * float(unit_candidate) - float(total)) <= 0.01 + abs(float(total)) * 0.005:
                        return qty_candidate, unit_candidate, total
                except ValueError:
                    continue
        unit = nums[-2]
        for cand in nums[:-2]:
            try:
                if float(cand) == int(float(cand)) and abs(float(cand) * float(unit) - float(total)) <= 0.01 + abs(float(total)) * 0.005:
                    return cand.split(".")[0], unit, total
            except ValueError:
                continue
        return nums[0].split(".")[0], unit, total
    
    if len(nums) == 2:
        # "Meal per head 90.00 ... 5400.00": qty hidden, recover 5400/90=60.
        try:
            unit, total = nums[-2], nums[-1]
            q = float(total) / float(unit) if float(unit) else 0
            if q > 0 and abs(q - round(q)) < 1e-9:
                return str(int(round(q))), unit, total
        except (ValueError, ZeroDivisionError):
            pass
        return "1", nums[-2], nums[-1]
    return "1", nums[-1], nums[-1]


def parse_row(row: str, page: str) -> dict:
    rate, rest = parse_rate(row)
    nums = _numbers_on(rest, thousands=False)
    qty, unit, total = "", "", ""
    desc = rest

    m = re.match(r"\s*(\d+)\s*[x×]\s*(.*)", rest, re.IGNORECASE)
    if m:
        qty, desc = m.group(1), m.group(2)
        nums = _numbers_on(desc, thousands=False)
        if nums:
            total = nums[-1]
            unit = nums[-2] if len(nums) >= 2 else nums[-1]
            for cand in nums[:-1]:
                try:
                    if abs(float(qty) * float(cand) - float(total)) <= 0.01:
                        unit = cand
                        break
                except ValueError:
                    continue
        else:
            unit = total = ""
    elif len(nums) >= 1:
        qty, unit, total = _pick_qty_unit(nums, desc)
    else:
        qty = unit = total = ""

    # Description = row minus numbers and rate, collapsed.
    desc_txt = re.sub(r"-?\d[\d.,]*", " ", desc).strip()
    desc_txt = re.sub(r"\s{2,}", " ", desc_txt).strip(" -|,;")
    # Absurd qty (registration numbers, phone numbers parsed as rows).
    try:
        if qty and float(qty) > 10000:
            return {"description": desc_txt[:150], "quantity": "", "unit_price": "",
                    "total": "", "tax_rate": rate, "raw": row[:200],
                    "page": page, "confidence": "low"}
    except ValueError:
        pass

    conf = "low"
    if qty and unit and total:
        try:
            ok = abs(round(float(qty) * float(unit), 2) - float(total)) <= 0.01 + abs(float(total)) * 0.005
            conf = "high" if ok else "medium"
            if not ok and not re.search(r"\d\s*(?:" + _QTY_UNIT_WORDS + r")\b", desc, re.IGNORECASE):
                # Weight/code misread as qty ("300g 24.99 ... 24.99"): fall back
                # to lump qty=1 x total — reversible, books the printed total.
                qty, unit = "1", total
                conf = "low"
        except ValueError:
            conf = "medium"
    elif total:
        conf = "medium"
        
    return {
        "description": desc_txt[:150],
        "quantity": qty,
        "unit_price": unit,
        "total": total,
        "tax_rate": rate,
        "raw": row[:200],
        "page": page,
        "confidence": conf,
    }


def extract_lines(page_texts: list[tuple[str, str]]) -> list[dict]:
    """All structured line candidates across kept pages."""
    out = []
    for pid, text in page_texts:
        tlines = text.splitlines()
        header, end = find_table_bounds(tlines)
        if header < 0:
            # No table: fall back to candidate rows. Skip totals, bank junk,
            # and tax-summary lines ("%") — those belong to taxes, not items.
            # Accept 2-number rows too ("90.00 ... 5400.00", qty recovered).
            for ln in tlines:
                if _scores(ln, _END_WORDS) >= 1 or _is_junk(ln) or "%" in ln:
                    continue
                if len(_numbers_on(ln)) >= 2 and len(ln.strip()) > 15:
                    # Without a table header for context, keep ONLY rows whose
                    # qty x unit == total (a transport annex like "Pag. 1/2"
                    # or a date "2026-06-15" never satisfies it).
                    r = parse_row(ln.strip(), pid)
                    if r["confidence"] == "high":
                        out.append(r)
            # "Nx description" list lines (INV-02 equipment list).
            for ln in tlines:
                if re.match(r"\s*\d+\s*[x×]\s*\S", ln):
                    r = parse_row(ln.strip(), pid)
                    if not any(o["raw"] == r["raw"] for o in out):
                        out.append(r)
            continue
        for row in assemble_rows(tlines, header, end):
            if len(row.strip()) < 3 or _is_junk(row):
                continue
            lowered = row.lower()
            if any(w in lowered for w in _NOTE_WORDS + _SUMMARY_WORDS):
                continue
            r = parse_row(row, pid)
            if len(r["description"]) < 2 and r["confidence"] != "high":
                continue  # column fragments ("A", "B") without proof
            out.append(r)
    # A row with no total books 0.00 in the ERP — noise, not an item.
    # A negative total from hyphenated codes ("2580-490" postcodes,
    # "2026-07-16" dates) is not a credit either — drop unless the row says
    # credit/storno/discount.
    # (A totals-region line bigger than the gross is filtered in build.py,
    # which knows the gross; lines.py only sees rows.)
    kept = []
    for o in out:
        if not o.get("total"):
            continue
        try:
            tot = float(o["total"])
            if tot == 0:
                continue  # "00:00h" timestamps parse as 0-totals; a truly free
                            # item is indistinguishable from noise — drop it.
            if tot < 0 and not re.search(
                    r"credit|storno|discount|rabatt", o["raw"], re.IGNORECASE):
                continue
        except ValueError:
            continue
        kept.append(o)
    return kept