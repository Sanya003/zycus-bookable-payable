from __future__ import annotations

import unittest

from src.build import extract_charges, extract_header_taxes
from src.extract import extract_dates, extract_totals, normalize_number
from src.lines import extract_lines, parse_row


class ParsingTests(unittest.TestCase):
    def test_exchange_rate_column_does_not_replace_unit_price(self) -> None:
        row = parse_row(
            "1 International Freight 567.000 KG USD 1.28 1.00000 725.76",
            "1",
        )
        self.assertEqual((row["quantity"], row["unit_price"], row["total"]),
                         ("567.000", "1.28", "725.76"))
        self.assertEqual(row["confidence"], "high")

    def test_tax_rate_comes_from_percentage_not_product_code(self) -> None:
        taxes = extract_header_taxes(
            "NHIL(2.5%) GHC 165.00\nCOVID-19 Levy (1%) GH 66.00\n"
            "VAT (15%) GHC 1049.40"
        )
        self.assertEqual([tax["tax_rate"] for tax in taxes], ["2.5", "1", "15"])
        self.assertEqual([tax["tax_amount"] for tax in taxes], ["165.00", "66.00", "1049.40"])

    def test_spaced_dates_are_normalized(self) -> None:
        dates = extract_dates(
            "Invoice Date: 2025 / 06 / 06\nDue Date: 2025 / 07 / 03", "1"
        )
        self.assertEqual(dates["invoice_date"]["value"], "2025-06-06")
        self.assertEqual(dates["due_date"]["value"], "2025-07-03")

    def test_currency_word_total_beats_customs_subtotal(self) -> None:
        result = extract_totals(
            "Total Liquidacao IVA 5.878,23\n"
            "Trinta e sete mil euros 37.767,32 euros",
            "1",
        )
        self.assertEqual(result["gross_total"]["value"], "37767.32")

    def test_amount_due_stays_in_document_currency(self) -> None:
        result = extract_totals(
            "Amount Due 1,040.06 USD\n"
            "Conversion to SGD for Tax purpose only\n"
            "Total Amount Due (SGD) 1,350.55",
            "1",
        )
        self.assertEqual(result["gross_total"]["value"], "1040.06")

    def test_invoice_total_beats_duty_rows(self) -> None:
        result = extract_totals(
            "Total Liquidacao IVA 5.878,23\n"
            "DIREITOS ANTI-DUMPING 31.889,09\n"
            "Total da factura 37.767,32",
            "1",
        )
        self.assertEqual(result["gross_total"]["value"], "37767.32")

    def test_total_payment_uses_final_aligned_amount(self) -> None:
        result = extract_totals(
            "GRAND TOTAL (INCLUDING VAT)\n"
            "TOTAL PAYMENT\n"
            "8,397.36\n"
            "8,161.92",
            "1",
        )
        self.assertEqual(result["gross_total"]["value"], "8161.92")

    def test_trailing_ocr_punctuation_is_ignored(self) -> None:
        self.assertEqual(normalize_number("8,161.92,"), "8161.92")

    def test_customs_summary_rows_are_not_invoice_lines(self) -> None:
        lines = extract_lines([
            ("1", "Code Description Qty Unit Total\n1 Import duties 1 31889.09 31889.09"),
            ("2", "Code Description Qty Unit Total\nTotal Liquidacao IVA 5878.23\nDIREITOS ANTI-DUMPING 31889.09"),
        ])
        self.assertEqual([line["total"] for line in lines], ["31889.09"])

    def test_explanatory_service_fee_is_not_a_charge(self) -> None:
        charges = extract_charges(
            "Total 37767.32\n"
            "Service fee of 40 EUR applies for every interest invoicing"
        )
        self.assertEqual(charges["extra_charges"], "")

    def test_bracket_heavy_customs_artifact_is_not_an_item(self) -> None:
        lines = extract_lines([
            ("1", "Description Quantity Unit Total\n"
                  "[ ] B [ | [ ] TR] ] | _ ] ] 1 6875.000 6875.000")
        ])
        self.assertEqual(lines, [])

    def test_tax_summary_row_is_not_an_item(self) -> None:
        lines = extract_lines([
            ("1", "Description Quantity Unit Total\n"
                  "% | INCIDENCIA | VALOR DESC. PROM/QNT 1 10.19 10.19")
        ])
        self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()
