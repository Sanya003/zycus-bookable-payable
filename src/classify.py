"""Classify pages and determine file status.

Page level: classify as INVOICE / CREDIT / ESTIMATE / DELIVERY / STATEMENT /
OTHER using keyword scoring; check CREDIT before INVOICE.

File level: combine page types, buyer evidence, and duplicate detection to
assign PAYABLE_CANDIDATE / DECLINED / REVIEW.
"""
from __future__ import annotations

import hashlib
import re

    # Keyword sets are matched case-insensitively.
_CREDIT = [
    "credit note", "credit memo", "credit invoice", "creditmemo",
    "gutschrift", "storno", "kreedit", "kreditarve",
    # bare "refund" deliberately excluded; it appears in return policies ("goods are neither refundable") on non-credit documents.
]
_ESTIMATE = [
    "estimate", "quotation", "proforma", "pro-forma", "angebot",
    "budget proposal", "sales quotation",
]
_DELIVERY = [
    "delivery note", "delivery slip", "lieferschein", "packing list",
    "delivery address", "goods received",
]
_STATEMENT = [
    "mahnung", "statement", "reminder", "dunning", "kontoauszug",
    "restbetrag", "payment reminder",
]
_INVOICE = [
    "invoice", "tax invoice", "rechnung", "fatura", "factura", "faktura",
    "arve", "lasku", "faktur", "nota fiscal",
]
# Bills that never use the word "invoice" (e.g. Ghana caterer "VENDOR DETAILS ... Total Tax Inclusive Value", Kenya "BILL TO ... PLEASE PAY").
_PAYABLE_SIGNALS = [
    "bill to", "please pay", "amount due", "tax exclusive",
    "total tax inclusive", "payment terms",
]
_BOLT_BUYERS = ["bolt", "northwind"]

_DUPLICATE_WORDS = ["duplicado", "duplicate", "copy", "kopie", "koopia"]


def _has(text: str, words: list[str]) -> str | None:
    t = text.lower()
    for w in words:
        if w in t:
            return w
    return None


def classify_page(text: str) -> dict:
    """Return {page_type, signal} for one page of OCR text."""
    hit = _has(text, _CREDIT)
    if hit:
        return {"page_type": "CREDIT", "signal": hit}
    
    hit = _has(text, _ESTIMATE)
    if hit:
        return {"page_type": "ESTIMATE", "signal": hit}
    
    hit = _has(text, _DELIVERY)
    if hit and not re.search(r"(total|betrag|amount due|endbetrag)", text.lower()):
        return {"page_type": "DELIVERY", "signal": hit}
    
    hit = _has(text, _STATEMENT)
    if hit:
        return {"page_type": "STATEMENT", "signal": hit}
    
    hit = _has(text, _INVOICE)
    if hit:
        return {"page_type": "INVOICE", "signal": hit}
    hit = _has(text, _PAYABLE_SIGNALS)
    
    if hit:
        return {"page_type": "INVOICE", "signal": hit + " (format signal)"}
    
    return {"page_type": "OTHER", "signal": ""}


def buyer_status(text: str) -> str:
    """Three-state buyer: 'bolt' | 'foreign' | 'unknown'.

    Why not a foreign-buyer blocklist: the held-back set will contain buyers
    we have never seen (DU-02's Novatek is already one). But every genuine
    Bolt payable names Northwind/Bolt as buyer, so we invert — candidacy must
    PROVE Bolt, not fail to prove foreign.
    """
    t = text.lower()
    if any(b in t for b in _BOLT_BUYERS):
        return "bolt"
    return "unknown"


def _norm(text: str) -> str:
    # Normalized fingerprint for duplicate detection: lowercase alnum only.
    return re.sub(r"[^a-z0-9]", "", text.lower())


def file_decision(filename: str, page_texts: list[str]) -> dict:
    """Combine page types + buyer + duplicates into a file verdict."""
    pages = [classify_page(t) for t in page_texts]
    types = [p["page_type"] for p in pages]
    full = "\n".join(page_texts)

    duplicates: list[int] = []
    seen: dict[str, int] = {}

    for i, t in enumerate(page_texts):
        h = hashlib.md5(_norm(t)[:2000].encode()).hexdigest()
        if h in seen:
            duplicates.append(i + 1)
        else:
            seen[h] = i + 1

    dup_word = _has(full, _DUPLICATE_WORDS)
    if dup_word and len(page_texts) > 1 and not duplicates:
        # "Original" on p1 + "Duplicado" on p2 with near-identical numbers.
        duplicates.append(len(page_texts))

    n_payable_pages = sum(1 for t in types if t in ("INVOICE", "CREDIT", "STATEMENT"))
    buyer = buyer_status(full)

    if n_payable_pages == 0:
        for t in ("ESTIMATE", "DELIVERY", "STATEMENT"):
            if t in types:
                dom = t
                break
        else:
            dom = "OTHER"

        why = {"ESTIMATE": "estimate/quotation, not a payable",
               "DELIVERY": "delivery notes, no payable content",
               "OTHER": "no invoice content found"}.get(dom, f"only {dom} pages")
        
        return _out(filename, pages, "DECLINED", why, duplicates)
    
    if "STATEMENT" in types and "INVOICE" not in types and "CREDIT" not in types:
        return _out(filename, pages, "REVIEW", "statement/dunning, needs line grouping", duplicates)
    
    if buyer == "unknown":
        return _out(filename, pages, "REVIEW", "buyer unconfirmed — verify before booking", duplicates)
    
    return _out(filename, pages, "PAYABLE_CANDIDATE", "invoice/credit content, Bolt buyer", duplicates)


def _out(filename, pages, verdict, reason, duplicates):
    return {
        "file": filename,
        "verdict": verdict,
        "reason": reason,
        "pages": pages,
        "duplicates": duplicates,
    }