"""Build payables from classified OCR pages.

Group duplicate pages; split only when >=2 distinct invoice numbers exist.
Otherwise, one file = one payable.

Follow AUTODRAFT_SCHEMA.md with printed totals, matched master codes, and
diagnostics. Map taxes, discounts, and charges from the totals region using
explicit printed amounts; never re-derive tax bases. Keep uncertain fields ""
rather than guessing.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from .extract import _numbers_on, extract_file
from .lines import extract_lines
from .match import Masters

_TAX_LINE_RES = [
    re.compile(r"(nhil|getfund|get fund|covid|vat|iva|mwst|gst|sst|moms|km|käibemaks|tax)\b[^\n]{0,40}?(\d+(?:[.,]\d+)?)\s*%[^\n]{0,30}?(-?[\d.,]+)\s*(?:ghs|ghc|eur|gbp|kes|zar|usd|chf)?", re.IGNORECASE),
]
_DISCOUNT_WORDS = ["allahindlus", "discount", "desc.", "rabatt", "soodustus", "less amount credited"]
_CHARGE_WORDS = {
    "freight_charges": ["freight", "delivery fee", "transport fee", "shipping"],
    "insurance_charges": ["insurance"],
    "extra_charges": ["fuel surcharge", "service fee", "handling"],
    "excise_duties": ["iec", "excise"],
}
_CHARGE_NOTE_WORDS = ["interest", "arrears", "every", "applies", "right to charge"]
_NEG_WORDS = ["withholding", "wht", "retention", "withheld", "deducted at source"]


def _f(x) -> float:
    try:
        return float(x)
    except (ValueError, TypeError):
        return 0.0


def _close(a: float, b: float, tol: float = 0.015) -> bool:
    return abs(a - b) <= tol


def decide_basis(lines: list[dict], printed_tax: float, printed_gross: float,
                 full_text: str = "") -> dict:
    evidence: list[str] = []
    text = (full_text or "").lower()
    label_vote = ""
    if any(h in text for h in ["incl", "brutto", "sisaldab", "including vat", "with vat", "inclusive", "ttc", "gross"]):
        label_vote = "GROSS"
        evidence.append("gross label printed")
    if any(h in text for h in ["excl", "neto", "net amount", "ilma käibemaksuta", "without vat", "exclusive", "price excl"]):
        if label_vote:
            label_vote = ""
            evidence.append("conflicting net/gross labels; using arithmetic")
        else:
            label_vote = "NET"
            evidence.append("net label printed")

    line_total = round(sum(_f(line.get("total")) for line in lines), 2)
    if line_total == 0 or printed_gross == 0:
        return {"basis": label_vote or "UNKNOWN",
                "evidence": evidence + ["no line/gross totals to test"]}
    net_ok = _close(line_total + printed_tax, printed_gross)
    gross_ok = _close(line_total, printed_gross) and printed_tax > 0.005
    if net_ok and not gross_ok:
        return {"basis": "NET", "evidence": evidence + [f"sum(lines)={line_total} + tax={printed_tax} == gross={printed_gross}"]}
    if gross_ok and not net_ok:
        return {"basis": "GROSS", "evidence": evidence + [f"sum(lines)={line_total} == gross={printed_gross} with tax printed"]}
    if net_ok and gross_ok:
        return {"basis": label_vote or "NET", "evidence": evidence + ["both hypotheses hold; label/default used"]}
    return {"basis": label_vote or "UNKNOWN",
            "evidence": evidence + [f"neither holds: sum={line_total} tax={printed_tax} gross={printed_gross}"]}


def convert_line_to_net(line: dict) -> dict:
    out = dict(line)
    rate = _f(line.get("tax_rate"))
    if rate <= 0 or not line.get("unit_price"):
        out["converted"] = False
        return out
    out["unit_price"] = f"{round(_f(line['unit_price']) / (1 + rate / 100.0), 2):.2f}"
    out["converted"] = True
    return out


def split_taxes(lines: list[dict], header_taxes: list[dict]) -> tuple[list[dict], list[dict]]:
    placed_lines = []
    for line in lines:
        item = dict(line)
        rate = (item.get("tax_rate") or "").strip()
        if rate and rate != "0":
            item["taxes"] = [{"tax_type": "VAT", "tax_name": "", "tax_rate": rate,
                               "tax_amount": "", "tax_type_code": ""}]
        elif rate == "0":
            item["taxes"] = [{"tax_type": "VAT", "tax_name": "0% / exempt", "tax_rate": "0",
                               "tax_amount": "0.00", "tax_type_code": ""}]
        else:
            item["taxes"] = []
        placed_lines.append(item)
    return placed_lines, header_taxes


def derived_line_tax_total(lines: list[dict]) -> float:
    total = 0.0
    for line in lines:
        for tax in line.get("taxes", []):
            rate = _f(tax.get("tax_rate"))
            if rate > 0 and not tax.get("tax_amount"):
                base = _f(line.get("quantity")) * _f(line.get("unit_price"))
                total += float(Decimal(str(base * rate / 100.0)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP))
    return round(total, 2)


def _tax_type(line: str) -> str:
    lowered = line.lower()
    for label, tax_type in (("nhil", "NHIL"), ("getfund", "GETFund"),
                            ("covid", "COVID"), ("sst", "SST"),
                            ("gst", "GST"), ("iva", "IVA")):
        if label in lowered:
            return tax_type
    return "VAT"


def extract_header_taxes(totals_region: str) -> list[dict]:
    """Taxes stated in the summary block, with printed amounts."""
    out = []
    for ln in totals_region.splitlines():
        low = ln.lower()
        neg = any(w in low for w in _NEG_WORDS)
        hit = False
        for pat in _TAX_LINE_RES:
            m = pat.search(ln)
            if m:
                rate = m.group(2).replace(",", ".")
                amount_values = _numbers_on(ln)
                if amount_values:
                    amt = amount_values[-1]
                    if neg and not amt.startswith("-"):
                        amt = "-" + amt
                    out.append({"tax_type": _tax_type(ln), "tax_name": ln.strip()[:80],
                                "tax_rate": rate, "tax_amount": amt,
                                "tax_type_code": ""})
                    hit = True
                    break
        if hit:
            continue
        # Wordless summary rows ("804 Incidência 41,68 23,00% 9,59"): accept
        # only if the printed triple reconciles (base x rate == amount).
        # That reconciliation IS the evidence — nothing is invented.
        m = re.search(r"(\d[\d.,]*)\s+(\d+[.,]\d+)\s*%\s+(-?[\d.,]+)", ln)
        if m:
            bnorm = _numbers_on(m.group(1))
            base = float(bnorm[-1]) if bnorm else 0.0
            rate = float(m.group(2).replace(",", "."))
            anorm = _numbers_on(m.group(3))
            amt = anorm[-1] if anorm else ""
            if amt and abs(round(base * rate / 100.0, 2) - float(amt)) <= 0.02:
                if neg and not amt.startswith("-"):
                    amt = "-" + amt
                out.append({"tax_type": _tax_type(ln), "tax_name": ln.strip()[:80],
                            "tax_rate": f"{rate}", "tax_amount": amt,
                            "tax_type_code": ""})
    # De-duplicate OCR echoes of the same summary line. Key includes the tax
    # name: Ghana's NHIL and GETFund are both 2.5% / 165.00 and both are owed.
    seen, uniq = set(), []
    for t in out:
        key = (t["tax_name"][:12].lower(), t["tax_rate"], t["tax_amount"])
        if key not in seen:
            seen.add(key)
            uniq.append(t)
    return uniq


def extract_header_discount(totals_region: str) -> tuple[str, str]:
    """Return (discount_amount, discount_pct) as printed, else ('','')."""
    for ln in totals_region.splitlines():
        if any(w in ln.lower() for w in _DISCOUNT_WORDS):
            nums = _numbers_on(ln)
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", ln)
            if m and nums:
                return nums[-1], m.group(1).replace(",", ".")
            if nums:
                return nums[-1], ""
    return "", ""


def extract_charges(totals_region: str) -> dict:
    out = {"freight_charges": "", "insurance_charges": "", "extra_charges": "", "excise_duties": ""}
    for ln in totals_region.splitlines():
        low = ln.lower()
        nums = _numbers_on(ln)
        if not nums or len(nums) >= 3 or any(word in low for word in _CHARGE_NOTE_WORDS):
            continue
        for field, words in _CHARGE_WORDS.items():
            if not out[field] and any(w in low for w in words):
                out[field] = nums[-1]
    return out


def _totals_region(full_text: str) -> str:
    markers = ["subtotal", "sub total", "vahesumma", "kokku", "tasuda", "endbetrag",
               "invoice total", "grand total", "kogusumma", "net amount", "total tax",
               "total", "levy", "levies", "nhil", "getfund", "covid", "käibemaks"]
    low = full_text.lower()
    pos = len(full_text)
    for mk in markers:
        i = low.find(mk)
        if i != -1:
            pos = min(pos, i)
    if pos == len(full_text):
        # No summary marker at all: summaries live at the bottom.
        return "\n".join(full_text.splitlines()[-25:])
    return full_text[pos:]


def _payable_summary_region(page_texts: list[tuple[str, str]], gross_raw: str,
                            full_text: str) -> str:
    """Return summary text from pages that print the selected payable total."""
    target = _f(gross_raw)
    matched = []
    for page, text in page_texts:
        page_total = extract_file([(page, text)])["totals"]["gross_total"]["value"]
        if page_total and abs(_f(page_total) - target) <= 0.005:
            matched.append(_totals_region(text))
    return "\n".join(matched) if matched else _totals_region(full_text)

def _days_between(d1: str, d2: str) -> int | None:
    try:
        return (datetime.strptime(d2, "%Y-%m-%d") - datetime.strptime(d1, "%Y-%m-%d")).days
    except ValueError:
        return None


def build_payable(page_texts: list[tuple[str, str]], page_types: list[str],
                  masters: Masters | None) -> dict:
    full = "\n".join(t for _, t in page_texts)
    ext = extract_file(page_texts)
    lines = extract_lines(page_texts)

    credit_pages = sum(1 for t in page_types if t == "CREDIT")
    inv_pages = sum(1 for t in page_types if t == "INVOICE")
    itype = "CREDIT_MEMO" if credit_pages > 0 and credit_pages >= inv_pages else "INVOICE"
    gross_raw = ext["totals"]["gross_total"]["value"]
    if itype == "CREDIT_MEMO" and gross_raw.startswith("-"):
        gross_raw = gross_raw[1:]  # schema: credit magnitudes are positive
    printed_gross = _f(gross_raw)
    region = _payable_summary_region(page_texts, gross_raw, full)
    header_taxes = extract_header_taxes(region)
    disc_amt, disc_pct = extract_header_discount(region)
    charges = extract_charges(region)
    printed_tax = round(sum(_f(t["tax_amount"]) for t in header_taxes), 2)
    # Sanity: no single line total can exceed the payable gross (a reference
    # number misread as a row, e.g. "credit invoice 0394289238"). Drop those.
    if printed_gross:
        lines = [ln for ln in lines if _f(ln.get("total")) <= printed_gross + 0.015]
        if len(lines) > 1:
            lines = [ln for ln in lines if abs(_f(ln.get("total")) - printed_gross) > 0.005]
    decision = decide_basis(lines, printed_tax, printed_gross, full)
    if decision["basis"] == "GROSS":
        lines = [convert_line_to_net(ln) for ln in lines]
    lines, header_taxes = split_taxes(lines, header_taxes)
    # Both per-line rates AND header summary amounts? Derivation drifts by
    # rounding (DU-06: derived 15.39 vs printed 15.38). Prefer the placement
    # that reproduces PRINTED amounts: if derivation mismatches, keep header
    # taxes with printed amounts and strip line taxes (no double count).
    if header_taxes and any(ln.get("taxes") for ln in lines):
        derived = derived_line_tax_total(lines)
        # Exact: even 1c of derivation drift breaks booking (DU-06: derived
        # 15.39 vs printed 15.38 -> gross 153.59 != 153.58).
        if abs(derived - printed_tax) > 0.005:
            for ln in lines:
                ln["taxes"] = []
            placement_note = (f"header placement: derived {derived} != printed {printed_tax}")
        else:
            header_taxes = []
            placement_note = "line placement: derivation matches printed summary"
    else:
        placement_note = "single placement source"
    line_total = round(sum(_f(ln.get("total")) for ln in lines), 2)
    header_tax_total = round(sum(_f(t.get("tax_amount")) for t in header_taxes), 2)
    tax_lines = [ln for ln in lines if re.search(r"\b(?:vat|iva|gst|tax|moms|sst)\b",
                                                  ln.get("description", ""), re.IGNORECASE)]
    if (header_taxes and abs(line_total - printed_gross) <= 0.005
            and tax_lines and any(abs(_f(ln.get("total")) - header_tax_total) <= 0.005
                                  for ln in tax_lines)):
        header_taxes = []
        placement_note = "header tax omitted: same amount is already an explicit tax line"
    inv_date = ext["dates"].get("invoice_date", {}).get("value", "")
    due_date = ext["dates"].get("due_date", {}).get("value", "")
    days = _days_between(inv_date, due_date) if inv_date and due_date else None
    supplier_name = ext["parties"]["supplier_name"]["value"]
    vat = (ext["parties"]["vat_ids"] or [""])[0]
    email = (ext["parties"]["emails"] or [""])[0]
    sid = masters.match_supplier(supplier_name, vat, email) if masters else ""
    country = masters.supplier_country(sid) if masters and sid else ""
    for t in header_taxes:
        if masters and country and not t["tax_type_code"]:
            t["tax_type_code"] = masters.match_tax(country, _f(t["tax_rate"]), t["tax_type"])
    for ln in lines:
        for t in ln.get("taxes", []):
            if masters and country and not t["tax_type_code"]:
                t["tax_type_code"] = masters.match_tax(country, _f(t["tax_rate"]))
    po_m = re.search(r"\bPO[-\s]?(?:EE|GH|MY|ZA|GB)?[-\s]?\d[\w\-]*", full, re.IGNORECASE)
    po_number = po_m.group(0).strip() if po_m else ""
    payable = {
        "invoice_number": ext["invoice_number"]["value"],
        "invoice_date": inv_date,
        "due_date": due_date,
        "invoice_type": itype,
        "currency": ext["currency"]["value"],
        "supplier": {"name": supplier_name, "supplier_id": sid,
                     "address": "", "vat_id": vat},
        "buyer": masters.match_buyer(full) if masters else {},
        "payment_term_id": masters.match_terms(full, days) if masters else "",
        "po_number": po_number,
        "po_id": masters.match_po(po_number) if masters and po_number else "",
        "gross_total": gross_raw,
        "subtotal": "",
        "total_tax_amount": f"{printed_tax:.2f}" if header_taxes else "",
        "discount_amount": disc_amt,
        "freight_charges": charges["freight_charges"],
        "insurance_charges": charges["insurance_charges"],
        "extra_charges": charges["extra_charges"],
        "excise_duties": charges["excise_duties"],
        "taxes": [{k: t[k] for k in ("tax_type", "tax_name", "tax_rate", "tax_amount", "tax_type_code")} for t in header_taxes],
        "line_items": [{
            "description": ln.get("description", ""),
            "item_type": "SERVICE",
            "uom": "",
            "quantity": ln.get("quantity", ""),
            "unit_price": ln.get("unit_price", ""),
            "total": ln.get("total", ""),
            "discount": "",
            "discount_percentage": disc_pct if len(lines) == 1 and disc_pct else "",
            "tax_rate": "",
            "tax_amount": "",
            "taxes": ln.get("taxes", []),
        } for ln in lines],
    }
    return {"payable": payable, "diagnostics": {
        "basis": decision["basis"], "evidence": decision["evidence"],
        "placement": placement_note,
        "n_lines": len(lines),
        "line_conf": [ln.get("confidence", "") for ln in lines],
        "warnings": [w for w in [
            "no lines extracted" if not lines else "",
            "gross total missing" if not printed_gross else "",
            "UNKNOWN pricing basis" if decision["basis"] == "UNKNOWN" else "",
        ] if w],
    }}


def build_file(filename: str, page_texts: list[tuple[str, str]], decision: dict,
               masters: Masters | None) -> dict:
    """One output-file dict: payables + declined, mirroring output contract."""
    if decision["verdict"] == "DECLINED":
        return {"file": filename, "payables": [],
                "declined": [{"doc_type": decision["pages"][0]["page_type"] if decision["pages"] else "OTHER",
                              "reason": decision["reason"]}], "diagnostics": {}}
    if decision["verdict"] == "REVIEW":
        return {"file": filename, "payables": [],
                "declined": [{"doc_type": "REVIEW", "reason": decision["reason"]}],
                "diagnostics": {"warnings": ["ambiguous document withheld from payable output"]}}
    # Drop duplicate pages (Original/Duplicado echoes).
    dup_idx = set(decision.get("duplicates", []))
    kept = [(pid, t) for i, (pid, t) in enumerate(page_texts, 1) if i not in dup_idx]
    kept_types = [p["page_type"] for i, p in enumerate(decision["pages"], 1) if i not in dup_idx]
    if not kept:
        kept, kept_types = page_texts, [p["page_type"] for p in decision["pages"]]
    number_res = re.compile(r"(?:rechnung\s*nr|invoice\s*(?:number|no)|FAC)\s*:?\s*([A-Za-z0-9\-/]{3,})", re.IGNORECASE)
    page_numbers = [(index, pid, text, number_res.findall(text))
                    for index, (pid, text) in enumerate(kept)]
    numbers = sorted({number.strip() for _, _, _, found in page_numbers for number in found})
    if len(numbers) > 1:
        groups = []
        declined = []
        for number in numbers:
            selected_rows = [(index, pid, text) for index, pid, text, found in page_numbers
                             if number in found]
            selected = [(pid, text) for _, pid, text in selected_rows]
            if selected:
                selected_types = [kept_types[index] for index, _, _ in selected_rows]
                group = build_payable(selected, selected_types, masters)
                if group["payable"].get("gross_total"):
                    groups.append(group)
                else:
                    declined.append({"doc_type": "REVIEW",
                                     "reason": f"invoice {number} has no grounded gross total"})
        payables = [group["payable"] for group in groups]
        diags = {"multi_payable": numbers,
                 "warnings": ["split by distinct printed invoice number"]}
        for group in groups:
            for key, value in group["diagnostics"].items():
                if key == "warnings":
                    diags.setdefault(key, []).extend(value)
                else:
                    diags.setdefault(key, value)
    else:
        b = build_payable(kept, kept_types, masters)
        if not b["payable"].get("gross_total"):
            return {"file": filename, "payables": [],
                    "declined": [{"doc_type": "REVIEW", "reason": "no grounded gross total"}],
                    "diagnostics": b["diagnostics"]}
        payables, diags = [b["payable"]], b["diagnostics"]
        declined = []
    return {"file": filename, "payables": payables, "declined": declined, "diagnostics": diags}