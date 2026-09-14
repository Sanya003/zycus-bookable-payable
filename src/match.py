"""match.py — Resolve document values to real master-data codes.

Build master indexes once; match without repeated full-table scans.
Use strict matches first, fuzzy only when confident; otherwise return "".

Supplier: VAT-ID > email > name (RapidFuzz >=90)
Tax: (country, rate) exact
Terms: alias in text > date difference (±2 days)
PO: exact po_number
Buyer: match Bolt company/unit/location by name tokens
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from rapidfuzz import fuzz


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


class Masters:
    def __init__(self, master_dir: Path):
        sup = json.loads((master_dir / "suppliers.json").read_text(encoding="utf-8"))
        tax = json.loads((master_dir / "tax_master.json").read_text(encoding="utf-8"))
        terms = json.loads((master_dir / "payment_terms.json").read_text(encoding="utf-8"))
        po = json.loads((master_dir / "po_master.json").read_text(encoding="utf-8"))
        cob = json.loads((master_dir / "chart_of_books.json").read_text(encoding="utf-8"))

        self.suppliers = sup.get("suppliers", [])
        self.taxes = tax.get("taxes", [])
        self.terms = terms.get("payment_terms", [])
        self.pos = {p["po_number"]: p["po_id"] for p in po.get("purchase_orders", [])}
        self._supplier_by_id = {s["supplier_id"]: s for s in self.suppliers}
        self._by_vat = {_norm(s.get("vat_id", "")): s["supplier_id"] for s in self.suppliers if s.get("vat_id")}
        self._by_email = {(s.get("email", "") or "").lower(): s["supplier_id"] for s in self.suppliers if s.get("email")}
        self._by_name = {_norm(s.get("name", "")): s["supplier_id"] for s in self.suppliers if s.get("name")}
        self._tax_by_key = {
            (t.get("country", ""), float(t.get("rate", -1)), t.get("tax_type", "").lower()): t.get("code", "")
            for t in self.taxes
        }
        self._term_by_alias = {
            alias.lower(): term["payment_term_id"]
            for term in self.terms
            for alias in term.get("text_aliases", [])
        }
        self._cob = cob.get("companies", [])

    # Supplier Match
    def match_supplier(self, name: str = "", vat_id: str = "", email: str = "") -> str:
        if vat_id and _norm(vat_id) in self._by_vat:
            return self._by_vat[_norm(vat_id)]
        
        for e in re.findall(r"[\w.\-]+@[\w.\-]+\.\w+", email or ""):
            if e.lower() in self._by_email:
                return self._by_email[e.lower()]
            
        if email and email.lower() in self._by_email:
            return self._by_email[email.lower()]

        normalized_name = _norm(name)
        if normalized_name in self._by_name:
            return self._by_name[normalized_name]
        
        best, best_score = "", 0
        for s in self.suppliers:
            score = fuzz.token_set_ratio(name or "", s.get("name", ""))
            if score > best_score:
                best, best_score = s["supplier_id"], score

        return best if best_score >= 90 else ""


    def supplier_country(self, supplier_id: str) -> str:
        return self._supplier_by_id.get(supplier_id, {}).get("country", "")


    # Tax Match
    def match_tax(self, country: str, rate: float, tax_type: str = "") -> str:
        if tax_type:
            return self._tax_by_key.get((country, rate, tax_type.lower()), "")
        for (tax_country, tax_rate, _), code in self._tax_by_key.items():
            if tax_country == country and abs(tax_rate - rate) < 1e-9:
                return code
        return ""


    # Payment Terms Match
    def match_terms(self, text: str = "", days_diff: int | None = None) -> str:
        t = (text or "").lower()

        for alias, payment_term_id in self._term_by_alias.items():
            if alias in t:
                return payment_term_id
                
        if days_diff is not None:
            best, best_d = "", 1e9
            for term in self.terms:
                d = abs(int(term.get("days", 0)) - days_diff)
                if d < best_d:
                    best, best_d = term["payment_term_id"], d

            if best_d <= 2:
                return best
            
        return ""


    # PO: exact only, a printed PO not in master is a non-ERP reference
    def match_po(self, po_number: str) -> str:
        return self.pos.get((po_number or "").strip(), "")


    # Buyer: resolve Bolt org codes from buyer text
    def match_buyer(self, text: str) -> dict[str, str]:
        t = (text or "").lower()
        out = {"company_code": "", "business_unit_code": "", "location_code": ""}

        for comp in self._cob:
            for bu in comp.get("business_units", []):
                names = (bu.get("business_unit_name", "") + " " + bu.get("business_unit_code", "")).lower()
                if any(tok in t for tok in names.split() if len(tok) > 3):
                    out = {
                        "company_code": comp.get("company_code", ""),
                        "business_unit_code": bu.get("business_unit_code", ""),
                        "location_code": (bu.get("locations", [{}])[0].get("location_code", "")),
                    }

        if "northwind" in t or "bolt" in t:
            out.setdefault("company_code", "BOLTGROUP")
            
            if not out["company_code"]:
                out["company_code"] = "BOLTGROUP"

        return out