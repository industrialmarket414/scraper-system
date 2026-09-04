import unittest

from panel1_b2b.scrapers.b2b_simtes import (
    SimtesScraper,
    _clean,
    _normalize_product_codes,
    _offer_bulk_url,
    _prefer_latest_product_rows,
    _summarize_offer_rows,
)


class SimtesScraperTests(unittest.TestCase):
    def test_clean_normalizes_portal_whitespace(self):
        self.assertEqual(_clean("  0309269397#932C\n -Rev0\xa0"), "0309269397#932C -Rev0")

    def test_normalizes_and_deduplicates_product_code_source(self):
        self.assertEqual(
            _normalize_product_codes([" VCF4 ", "", "# kaynak", "vcf4", "LRD03"]),
            ["VCF4", "LRD03"],
        )

    def test_pending_product_codes_excludes_checked_codes(self):
        scraper = SimtesScraper({
            "id": "simtes",
            "name": "Simtes",
            "url": "https://example.test",
        })
        scraper.product_codes = lambda: ["VCF4", "LRD03", "LC1DMKM7"]
        scraper.checked_product_codes_path = self._write_temp_codes(["vcf4", "LC1DMKM7"])
        try:
            self.assertEqual(scraper.pending_product_codes(), ["LRD03"])
        finally:
            scraper.checked_product_codes_path.unlink(missing_ok=True)

    def test_manual_login_timeout_has_safe_minimum(self):
        scraper = SimtesScraper({
            "id": "simtes",
            "name": "Simtes",
            "url": "https://example.test",
            "manual_login_timeout_seconds": 5,
        })
        self.assertEqual(scraper.manual_login_timeout_ms, 60_000)

    def test_brand_specific_product_code_files_are_separate(self):
        schneider = self._write_temp_codes(["A9F74116"])
        siemens = self._write_temp_codes(["3RV2011-1AA10"])
        schneider_checked = self._write_temp_codes([])
        siemens_checked = self._write_temp_codes(["3RV2011-1AA10"])
        scraper = SimtesScraper({
            "id": "simtes",
            "name": "Simtes",
            "url": "https://example.test",
            "product_code_files": {
                "Schneider Electric": str(schneider),
                "Siemens": str(siemens),
            },
            "checked_product_code_files": {
                "Schneider Electric": str(schneider_checked),
                "Siemens": str(siemens_checked),
            },
        })
        try:
            self.assertEqual(scraper.product_codes("Schneider Electric"), ["A9F74116"])
            self.assertEqual(scraper.product_codes("Siemens"), ["3RV2011-1AA10"])
            self.assertEqual(scraper.pending_product_codes("Siemens"), [])
        finally:
            for path in (schneider, siemens, schneider_checked, siemens_checked):
                path.unlink(missing_ok=True)

    def test_brand_specific_offer_url_is_selected(self):
        scraper = SimtesScraper({
            "id": "simtes",
            "name": "Simtes",
            "url": "https://example.test",
            "offer_urls": {
                "Schneider Electric": "https://example.test/schneider",
                "Siemens": "https://example.test/siemens",
            },
        })

        self.assertEqual(
            scraper.offer_url("Siemens"),
            "https://example.test/siemens",
        )

    def test_offer_bulk_url_keeps_offer_token(self):
        self.assertEqual(
            _offer_bulk_url("https://example.test/add-product-to-offer/TOKEN"),
            "https://example.test/offer-from-excel/TOKEN",
        )

    @staticmethod
    def _write_temp_codes(codes):
        from pathlib import Path
        import tempfile

        handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False)
        with handle:
            handle.write("\n".join(codes))
        return Path(handle.name)

    def test_summarizes_100_unit_stock_split(self):
        rows = [
            {
                "product_code": "VCF4",
                "stock": "Terminli",
                "stock_status": "Terminli",
                "quantity": "91",
                "offer_code": "NEW",
                "discount": "-",
            },
            {
                "product_code": "VCF4",
                "stock": "Var",
                "stock_status": "Var",
                "quantity": "9",
                "offer_code": "NEW",
                "discount": "63",
            },
        ]

        result = _summarize_offer_rows(rows)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["stock"], "9")
        self.assertEqual(result[0]["stock_status"], "Var")
        self.assertEqual(result[0]["discount"], "63")

    def test_keeps_first_latest_offer_when_older_row_has_stock(self):
        rows = [
            {"product_code": "A9R", "stock": "Terminli", "quantity": "1", "offer_code": "NEW"},
            {"product_code": "A9R", "stock": "Var", "quantity": "1", "offer_code": "OLD"},
        ]
        self.assertEqual(_prefer_latest_product_rows(rows)[0]["offer_code"], "NEW")

    def test_reports_100_without_plus_when_all_requested_units_are_in_stock(self):
        rows = [{
            "product_code": "GZ1E04",
            "stock": "Var",
            "stock_status": "Var",
            "quantity": "100",
            "offer_code": "NEW",
        }]

        result = _summarize_offer_rows(rows)

        self.assertEqual(result[0]["stock"], "100")
        self.assertEqual(result[0]["stock_status"], "Var")


if __name__ == "__main__":
    unittest.main()
