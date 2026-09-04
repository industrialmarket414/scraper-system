import unittest
from urllib.parse import parse_qs, urlsplit

from panel1_b2b.scrapers.b2b_anka import _product_api_url


class AnkaScraperTests(unittest.TestCase):
    def test_product_api_url_preserves_columns_and_updates_page(self):
        template = (
            "https://b2b.ankamuh.com/products?"
            "columns%5B0%5D%5Bdata%5D=brand&"
            "columns%5B1%5D%5Bdata%5D=code&"
            "brand_id=284&start=0&length=10"
        )

        result = _product_api_url(template, "1", 200, 100)
        query = parse_qs(urlsplit(result).query)

        self.assertEqual(query["columns[0][data]"], ["brand"])
        self.assertEqual(query["columns[1][data]"], ["code"])
        self.assertEqual(query["brand_id"], ["1"])
        self.assertEqual(query["start"], ["200"])
        self.assertEqual(query["length"], ["100"])


if __name__ == "__main__":
    unittest.main()
