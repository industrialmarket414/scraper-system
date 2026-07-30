import unittest
import sqlite3
import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from openpyxl import load_workbook
from core.config_manager import _DEFAULT, _apply_settings_migrations, get_mapping_sites
from panel2_fiyat import database as price_database
from panel2_fiyat import excel_exporter
from panel2_fiyat.comparison import _discount_offer
from panel2_fiyat.scrapers._generic import _infer_product_code, _parse_tr_price
from panel2_fiyat.scrapers.site_elektromarketim import (
    _parse_products as _parse_elektromarketim_products,
)
from panel2_fiyat.scrapers.site_elektrix import (
    _parse_products as _parse_elektrix_products,
)
from panel2_fiyat.scrapers.site_elektrikmarket import (
    _detail_values as _elektrikmarket_detail_values,
    _parse_category_products as _parse_elektrikmarket_products,
)
from panel2_fiyat.scrapers.site_elektrikmalzemesi import (
    _parse_category_products as _parse_elektrikmalzemesi_products,
)
from panel2_fiyat.scrapers.site_elektrikciden import (
    _parse_products as _parse_elektrikciden_products,
)
from panel2_fiyat.scrapers.site_ucfaz import (
    _parse_products as _parse_ucfaz_products,
)
from panel2_fiyat.scrapers.site_aydinlatmadunyam import (
    _parse_product as _parse_aydinlatmadunyam_product,
)
from panel2_fiyat.scrapers.site_cimenelektrik import (
    _parse_product as _parse_cimenelektrik_product,
)
from panel2_fiyat.scrapers.site_kozaydinlatma import (
    _parse_product as _parse_kozaydinlatma_product,
    _sitemap_product_urls as _koz_sitemap_product_urls,
)
from panel2_fiyat.scrapers.site_krcelektromarket import (
    _parse_products as _parse_krcelektromarket_products,
)
from panel2_fiyat.scrapers.site_buyukalpler import (
    _parse_product as _parse_buyukalpler_product,
)
from panel2_fiyat.scrapers.site_starakim import _detail_data, _product_code
from panel2_fiyat.scrapers.site_tumpaelektrik import (
    _parse_products as _parse_tumpaelektrik_products,
)
from panel2_fiyat.scrapers.site_zeybekmarket import (
    _parse_products as _parse_zeybekmarket_products,
)


class DiscountOfferTests(unittest.TestCase):
    def test_calculates_discount_to_beat_cheapest_competitor(self):
        offer = _discount_offer(
            {"price": 90.0, "list_price": 100.0},
            [
                {"site_id": "a", "site_name": "A", "price": 82.0},
                {"site_id": "b", "site_name": "B", "price": 80.0},
            ],
        )
        self.assertEqual(offer["competitor_site_id"], "b")
        self.assertEqual(offer["target_price"], 79.99)
        self.assertEqual(offer["required_total_discount_percent"], 20.01)
        self.assertEqual(offer["additional_reduction_amount"], 10.01)
        self.assertEqual(offer["additional_discount_percent"], 11.12)
        self.assertFalse(offer["already_best"])

    def test_no_extra_discount_when_we_are_already_cheapest(self):
        offer = _discount_offer(
            {"price": 70.0, "old_price": 100.0},
            [{"site_id": "a", "site_name": "A", "price": 80.0}],
        )
        self.assertTrue(offer["already_best"])
        self.assertEqual(offer["target_price"], 70.0)
        self.assertEqual(offer["additional_discount_percent"], 0.0)
        self.assertEqual(offer["required_total_discount_percent"], 30.0)

    def test_missing_list_price_still_calculates_extra_discount(self):
        offer = _discount_offer(
            {"price": 120.0},
            [{"site_id": "a", "site_name": "A", "price": 100.0}],
        )
        self.assertIsNone(offer["required_total_discount_percent"])
        self.assertEqual(offer["target_price"], 99.99)
        self.assertEqual(offer["additional_discount_percent"], 16.68)


class GenericParserTests(unittest.TestCase):
    def test_turkish_price(self):
        self.assertEqual(_parse_tr_price("1.234,56 TL"), 1234.56)

    def test_model_code_from_name(self):
        self.assertEqual(
            _infer_product_code("Cata CT-6060 Plazma Isıtıcı 2000W"),
            "CT-6060",
        )
        self.assertEqual(
            _infer_product_code("Schneider A9F74116 Otomatik Sigorta 16A"),
            "A9F74116",
        )
        self.assertEqual(_product_code("Audio 001196 Villa Set", "Audio"), "001196")
        self.assertEqual(_product_code("Asel 2800 W Isıtıcı", "Asel"), "")

    def test_spec_is_not_treated_as_code(self):
        self.assertEqual(_infer_product_code("Philips 8W Ampul 6500K 220V"), "")


class SettingsTests(unittest.TestCase):
    def test_competitor_migration_is_idempotent(self):
        settings = deepcopy(_DEFAULT)
        self.assertTrue(_apply_settings_migrations(settings))
        self.assertEqual(len(settings["price_sites"]), 9)
        self.assertFalse(_apply_settings_migrations(settings))
        self.assertEqual(len(settings["price_sites"]), 9)

    def test_mapping_sites_include_b2b_and_price_sites(self):
        settings = {
            "b2b_sites": [{"id": "same", "name": "B2B"}],
            "price_sites": [
                {"id": "same", "name": "Duplicate"},
                {"id": "competitor", "name": "Competitor"},
            ],
        }
        self.assertEqual(
            [site["id"] for site in get_mapping_sites(settings)],
            ["same", "competitor"],
        )


class PriceDatabaseTests(unittest.TestCase):
    def test_retention_keeps_complete_scrape_batches(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE price_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id TEXT, site_name TEXT, product_code TEXT,
                product_name TEXT, brand TEXT, category TEXT, price REAL,
                old_price REAL, list_price REAL, discount_percent REAL,
                currency TEXT, is_bestseller INTEGER, bestseller_label TEXT,
                em_code TEXT, url TEXT, scraped_at TEXT
            )"""
        )
        with (
            patch.object(price_database, "get_conn", return_value=conn),
            patch.object(price_database, "load_settings", return_value={"price_max_records": 2}),
            patch("core.product_mapping.load_mapping_for_site", return_value={}),
        ):
            for batch in range(3):
                price_database.save_products(
                    "site", "Site",
                    [
                        {"product_code": f"{batch}-A", "product_name": "A", "price": 10},
                        {"product_code": f"{batch}-B", "product_name": "B", "price": 20},
                    ],
                )

        rows = conn.execute(
            "SELECT product_code FROM price_records ORDER BY product_code"
        ).fetchall()
        self.assertEqual([row["product_code"] for row in rows], ["1-A", "1-B", "2-A", "2-B"])
        conn.close()


class PriceExcelExporterTests(unittest.TestCase):
    def test_exports_typed_price_and_discount_columns(self):
        with TemporaryDirectory() as temporary_dir:
            with patch.object(excel_exporter, "OUTPUT_DIR", Path(temporary_dir)):
                path = excel_exporter.export_price_excel(
                    "Star Akım",
                    "starakim",
                    [{
                        "brand": "ACK",
                        "product_code": "A-1",
                        "product_name": "Test Ürün",
                        "stock": "Stokta",
                        "discount_percent": 25,
                        "list_price": 100.0,
                        "price": 75.0,
                        "category": "Aydınlatma",
                        "url": "https://example.com/urun",
                        "scraped_at": "2026-07-27 10:00:00",
                    }],
                )
            workbook = load_workbook(path, data_only=False)
            sheet = workbook.active
            self.assertEqual(sheet.max_row, 2)
            self.assertEqual(sheet["E2"].value, 0.25)
            self.assertEqual(sheet["F2"].value, 100.0)
            self.assertEqual(sheet["G2"].value, 75.0)
            self.assertEqual(sheet["I2"].hyperlink.target, "https://example.com/urun")
            workbook.close()


class StarakimStockTests(unittest.TestCase):
    def test_reads_exact_quantity_from_product_data(self):
        html = """
        <input id="product-id" value="130235">
        <script>
        PRODUCT_DATA.push(JSON.parse('{\\\"id\\\":\\\"130235\\\",\\\"name\\\":\\\"2\\'li Kumanda\\\",\\\"supplier_code\\\":\\\"ACK-AT20-01233\\\",\\\"available\\\":true,\\\"quantity\\\":6}'));
        </script>
        """
        data = _detail_data(html)
        self.assertEqual(data["quantity"], 6)
        self.assertEqual(data["supplier_code"], "ACK-AT20-01233")


class ElektromarketimStockTests(unittest.TestCase):
    def test_reads_stock_price_and_code_from_category_source(self):
        html = """
        <script>
        PRODUCT_DATA.push(JSON.parse('{\\\"id\\\":\\\"56503\\\",\\\"name\\\":\\\"Global K2 Ampul\\\",\\\"supplier_code\\\":\\\"KES222-9WBEYAZ\\\",\\\"total_base_price\\\":189,\\\"total_sale_price\\\":85.05,\\\"quantity\\\":740,\\\"url\\\":\\\"global-k2-ampul\\\",\\\"brand\\\":\\\"Global\\\",\\\"category\\\":\\\"Led Ampuller\\\",\\\"category_path\\\":\\\"Ayd\\\\u0131nlatma > Ampuller > \\\"}'));
        </script>
        """
        rows = _parse_elektromarketim_products(
            html,
            "Aydınlatma",
            "2026-07-27 12:00:00",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stock_quantity"], 740)
        self.assertEqual(rows[0]["product_code"], "KES222-9WBEYAZ")
        self.assertEqual(rows[0]["price"], 85.05)
        self.assertEqual(rows[0]["discount_percent"], 55.0)


class ElektrixStockTests(unittest.TestCase):
    def test_reads_stock_price_and_code_from_category_source(self):
        html = """
        <script>
        PRODUCT_DATA.push(JSON.parse('{\\\"id\\\":\\\"42044\\\",\\\"name\\\":\\\"PAR16 LED AMPUL\\\",\\\"code\\\":\\\"OSRAM-LED-PAR16-5W-84GU\\\",\\\"supplier_code\\\":\\\"OSRAM-LED-PAR16-5W-84GU\\\",\\\"total_base_price\\\":46.91,\\\"total_sale_price\\\":38.4,\\\"quantity\\\":5,\\\"url\\\":\\\"par16-led-ampul\\\",\\\"brand\\\":\\\"Osram\\\",\\\"category\\\":\\\"Led Ampul\\\",\\\"category_path\\\":\\\"Ayd\\\\u0131nlatma > \\\"}'));
        </script>
        """
        rows = _parse_elektrix_products(
            html,
            "AydÄ±nlatma",
            "2026-07-27 13:00:00",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stock_quantity"], 5)
        self.assertEqual(rows[0]["product_code"], "OSRAM-LED-PAR16-5W-84GU")
        self.assertEqual(rows[0]["price"], 38.4)
        self.assertEqual(rows[0]["discount_percent"], 18.14)


class ElektrikmarketStockTests(unittest.TestCase):
    def test_reads_card_price_and_exact_detail_stock(self):
        html = """
        <div class="showcase">
          <div class="showcase-brand"><a>Cata</a></div>
          <div class="showcase-title">
            <a href="/urun/cenevre" title="Cata Cenevre CT-3000">Ürün</a>
          </div>
          <div class="showcase-price-old">9.900,00 TL</div>
          <div class="showcase-price-new">3.960,00 TL KDV DAHİL</div>
          <a class="add-to-cart-button" data-product-id="239095"></a>
        </div>
        """
        rows = _parse_elektrikmarket_products(
            html,
            "Elektronik",
            "2026-07-27 14:00:00",
        )
        self.assertEqual(rows[0]["brand"], "Cata")
        self.assertEqual(rows[0]["list_price"], 9900.0)
        self.assertEqual(rows[0]["price"], 3960.0)
        self.assertEqual(rows[0]["discount_percent"], 60.0)

        detail = """
        <script>pageParams = {product: {id: "239095",sku: "CT-3000"}}</script>
        <input data-stockamount='1085'>
        """
        self.assertEqual(
            _elektrikmarket_detail_values(detail),
            ("CT-3000", 1085),
        )


class ElektrikMalzemesiStockTests(unittest.TestCase):
    def test_reads_showcase_metadata_prices_and_availability(self):
        items = [{
            "id": 400874,
            "sku": "66748 ECO",
            "fullName": "OSRAM 48W HALOPİN-EKO-ŞEFFAF OSRAM",
            "brandName": "OSRAM",
            "categoryName": "Halojen Ampul",
        }]
        html = f"""
        <div class="showcase">
          <div class="showcase-title">
            <a href="/urun/osram-48w-halopin-eko-seffaf-osram"
               title="OSRAM 48W HALOPİN-EKO-ŞEFFAF OSRAM">Ürün</a>
          </div>
          <div class="showcase-price-new">123,00 TL</div>
          <div class="showcase-price-old">185,00 TL</div>
          <a class="add-to-cart-button" data-product-id="400874"></a>
        </div>
        <script>var showcaseItems = {json.dumps(items)};</script>
        """
        row = _parse_elektrikmalzemesi_products(
            html,
            "Aydınlatma",
            "2026-07-27 17:00:00",
        )[0]
        self.assertEqual(row["brand"], "OSRAM")
        self.assertEqual(row["product_code"], "66748 ECO")
        self.assertEqual(row["category"], "Halojen Ampul")
        self.assertEqual(row["price"], 123.0)
        self.assertEqual(row["list_price"], 185.0)
        self.assertEqual(row["discount_percent"], 33.51)
        self.assertTrue(row["_expects_stock"])


class ElektrikcidenStockTests(unittest.TestCase):
    def test_reads_vat_inclusive_prices_and_exact_stock(self):
        payload = {"PRODUCTS": [{
            "ID": "9249",
            "TITLE": "Depa 12x12mm Yapışkanlı Plastik Kablo Kanalı",
            "IS_ACTIVE": "1",
            "BRAND": "Depa",
            "PRODUCT_CODE": "TKY-B01",
            "PRICE_SELL": "8.98",
            "PRICE_NOT_DISCOUNTED": 17.95,
            "VAT": 20,
            "DISPLAY_VAT": "1",
            "DISCOUNT_PERCENT_RAW": 49.97,
            "STOCK": "974",
            "IN_STOCK": True,
            "CATEGORY_PATH": "Tesisat Malzemeleri > Kablo Kanalları > ",
            "CATEGORY_NAME": "Yapışkanlı Plastik Kablo Kanalları",
            "URL": "depa-12x12mm-yapiskanli-plastik-kablo-kanali-1m",
        }]}
        row = _parse_elektrikciden_products(
            payload,
            "2026-07-27 18:00:00",
        )[0]
        self.assertEqual(row["brand"], "Depa")
        self.assertEqual(row["stock_quantity"], 974)
        self.assertEqual(row["stock"], "Stokta")
        self.assertEqual(row["list_price"], 21.54)
        self.assertEqual(row["price"], 10.78)
        self.assertEqual(row["discount_percent"], 49.97)
        self.assertEqual(
            row["category"],
            "Tesisat Malzemeleri > Kablo Kanalları > "
            "Yapışkanlı Plastik Kablo Kanalları",
        )

    def test_negative_stock_is_preserved_but_unavailable(self):
        payload = {"PRODUCTS": [{
            "TITLE": "Test Ürün",
            "IS_ACTIVE": "1",
            "BRAND": "TEST",
            "PRICE_SELL": 100,
            "PRICE_NOT_DISCOUNTED": 100,
            "VAT": 20,
            "DISPLAY_VAT": "1",
            "STOCK": "-7",
            "IN_STOCK": True,
            "URL": "test-urun",
        }]}
        row = _parse_elektrikciden_products(
            payload,
            "2026-07-27 18:00:00",
        )[0]
        self.assertEqual(row["stock_quantity"], -7)
        self.assertEqual(row["stock"], "Tükendi")


class UcFazStockTests(unittest.TestCase):
    def test_reads_vat_inclusive_prices_supplier_code_and_exact_stock(self):
        payload = {"PRODUCTS": [{
            "ID": "59015",
            "TITLE": "Asfora RC 370W Vavien Led Dimmer Beyaz",
            "IS_ACTIVE": "1",
            "BRAND": "Schneider Electric",
            "PRODUCT_CODE": "T59015",
            "SUPPLIER_PRODUCT_CODE": "EPH6870121",
            "PRICE_SELL": 1577.175576,
            "PRICE_NOT_DISCOUNTED": 3942.93894,
            "VAT": 20,
            "DISPLAY_VAT": "0",
            "DISCOUNT_PERCENT_RAW": 60,
            "STOCK": "3",
            "IN_STOCK": True,
            "CATEGORY_PATH": "EV ELEKTRİĞİ > Sıva Altı Anahtar-Priz >",
            "CATEGORY_NAME": "Schneider Asfora",
            "URL": "asfora-rc-370w-vavien-led-dimmer-beyaz",
        }]}
        row = _parse_ucfaz_products(
            payload,
            "2026-07-27 19:00:00",
        )[0]
        self.assertEqual(row["brand"], "Schneider Electric")
        self.assertEqual(row["product_code"], "EPH6870121")
        self.assertEqual(row["stock_quantity"], 3)
        self.assertEqual(row["stock"], "Stokta")
        self.assertEqual(row["list_price"], 4731.53)
        self.assertEqual(row["price"], 1892.61)
        self.assertEqual(row["discount_percent"], 60)
        self.assertEqual(
            row["category"],
            "EV ELEKTRİĞİ > Sıva Altı Anahtar-Priz > Schneider Asfora",
        )

    def test_invalid_source_list_price_falls_back_to_sale_price(self):
        payload = {"PRODUCTS": [{
            "TITLE": "10 MT Kablolu 9 Duylu Bahçe Seti",
            "IS_ACTIVE": "1",
            "BRAND": "3faz",
            "PRODUCT_CODE": "3FÖA1109-010",
            "PRICE_SELL": 1750,
            "PRICE_NOT_DISCOUNTED": 500,
            "VAT": 20,
            "DISCOUNT_PERCENT_RAW": 0,
            "STOCK": "1000",
            "IN_STOCK": True,
            "URL": "bahce-seti",
        }]}
        row = _parse_ucfaz_products(
            payload,
            "2026-07-27 19:00:00",
        )[0]
        self.assertEqual(row["list_price"], 2100)
        self.assertEqual(row["price"], 2100)
        self.assertEqual(row["discount_percent"], 0)

    def test_negative_stock_is_preserved_but_unavailable(self):
        payload = {"PRODUCTS": [{
            "TITLE": "Test Ürün",
            "IS_ACTIVE": "1",
            "BRAND": "TEST",
            "PRICE_SELL": 100,
            "PRICE_NOT_DISCOUNTED": 100,
            "VAT": 20,
            "STOCK": "-2",
            "IN_STOCK": True,
            "URL": "test-urun",
        }]}
        row = _parse_ucfaz_products(
            payload,
            "2026-07-27 19:00:00",
        )[0]
        self.assertEqual(row["stock_quantity"], -2)
        self.assertEqual(row["stock"], "Tükendi")


class AydinlatmaDunyamStockTests(unittest.TestCase):
    def test_reads_detail_metadata_vat_prices_and_exact_stock(self):
        html = """
        <script>
        pageParams = {product: {
          id: "128758",
          sku: "Bosch-2608644377",
          fullName: "Bosch Daire Testere Biçagi 190*30 Mm 48 Dis",
          categoryName: "El Aletleri",
          brandName: "Bosch",
          priceWithCurrency: 455.0000,
          quantity: 1,
          salePrice: 491.4,
          tax: 20
        }};
        </script>
        <input data-stockamount='5'>
        """
        row = _parse_aydinlatmadunyam_product(
            html,
            "https://www.aydinlatmadunyam.com/urun/test",
            "2026-07-27 20:00:00",
        )
        self.assertEqual(row["brand"], "Bosch")
        self.assertEqual(row["product_code"], "Bosch-2608644377")
        self.assertEqual(row["stock_quantity"], 5)
        self.assertEqual(row["stock"], "Stokta")
        self.assertEqual(row["list_price"], 546)
        self.assertEqual(row["price"], 491.4)
        self.assertEqual(row["discount_percent"], 10)
        self.assertEqual(row["category"], "El Aletleri")

    def test_negative_stock_is_preserved_but_unavailable(self):
        html = """
        <script>
        pageParams = {product: {
          sku: "TEST-1",
          fullName: "Test Ürün",
          categoryName: "Test",
          brandName: "Test",
          priceWithCurrency: 100,
          salePrice: 120,
          tax: 20
        }};
        </script>
        <input data-stockamount='-3'>
        """
        row = _parse_aydinlatmadunyam_product(
            html,
            "https://www.aydinlatmadunyam.com/urun/test",
            "2026-07-27 20:00:00",
        )
        self.assertEqual(row["stock_quantity"], -3)
        self.assertEqual(row["stock"], "Tükendi")


class CimenElektrikStockTests(unittest.TestCase):
    def test_reads_ticimax_metadata_vat_prices_and_exact_stock(self):
        model = {
            "productName": "CATA CT 4215 Led Çanak Ampul",
            "stockCode": "CT-4215-26923",
            "productPriceKDVIncluded": 23.52,
            "productCurrency": "TRY",
            "brandName": "CATA",
            "totalStockAmount": 2043,
            "product": {
                "stokKodu": "CT-4215-26923",
                "stokAdedi": 2043,
                "satisFiyati": 49,
                "satisKDV": 9.8,
                "indirimliFiyati": 19.6,
                "indirimliKDV": 3.92,
            },
            "breadCrumb": [
                {"tanim": "Led Çanak Ampuller"},
                {"tanim": "AMPULLER"},
            ],
        }
        html = f"<script>var productDetailModel = {json.dumps(model)};</script>"
        row = _parse_cimenelektrik_product(
            html,
            "https://www.cimenelektrik.com/test-25429",
            "2026-07-27 21:00:00",
        )
        self.assertEqual(row["brand"], "CATA")
        self.assertEqual(row["product_code"], "CT-4215-26923")
        self.assertEqual(row["stock_quantity"], 2043)
        self.assertEqual(row["stock"], "Stokta")
        self.assertEqual(row["list_price"], 58.8)
        self.assertEqual(row["price"], 23.52)
        self.assertEqual(row["discount_percent"], 60)
        self.assertEqual(row["category"], "AMPULLER > Led Çanak Ampuller")

    def test_negative_stock_is_preserved_but_unavailable(self):
        model = {
            "productName": "Test Ürün",
            "stockCode": "TEST-1",
            "productPriceKDVIncluded": 120,
            "brandName": "Test",
            "totalStockAmount": -5,
            "product": {
                "stokAdedi": -5,
                "satisFiyati": 100,
                "satisKDV": 20,
                "indirimliFiyati": 100,
                "indirimliKDV": 20,
            },
        }
        html = f"<script>var productDetailModel = {json.dumps(model)};</script>"
        row = _parse_cimenelektrik_product(
            html,
            "https://www.cimenelektrik.com/test",
            "2026-07-27 21:00:00",
        )
        self.assertEqual(row["stock_quantity"], -5)
        self.assertEqual(row["stock"], "Tükendi")


class KozAydinlatmaStockTests(unittest.TestCase):
    def test_reads_detail_vat_prices_and_exact_stock(self):
        html = """
        <script>
        pageParams = {product: {
          sku: "ct-4664",
          fullName: "Cata 300W Platinum Led Projektör",
          categoryName: "Led Projektör",
          brandName: "Cata",
          priceWithCurrency: 2750,
          salePrice: 1518,
          tax: 20
        }};
        </script>
        <input data-stockamount='36'>
        """
        row = _parse_kozaydinlatma_product(
            html,
            "https://www.kozaydinlatma.com/urun/test",
            "2026-07-28 09:00:00",
        )
        self.assertEqual(row["brand"], "Cata")
        self.assertEqual(row["product_code"], "ct-4664")
        self.assertEqual(row["stock_quantity"], 36)
        self.assertEqual(row["stock"], "Stokta")
        self.assertEqual(row["list_price"], 3300)
        self.assertEqual(row["price"], 1518)
        self.assertEqual(row["discount_percent"], 54)

    def test_retries_temporarily_empty_product_sitemap(self):
        sitemap = """
        <urlset>
          <url><loc>https://www.kozaydinlatma.com/urun/test-1</loc></url>
          <url><loc>https://www.kozaydinlatma.com/urun/test-2</loc></url>
        </urlset>
        """
        with (
            patch(
                "panel2_fiyat.scrapers.site_kozaydinlatma._fetch_text",
                side_effect=["", sitemap],
            ),
            patch("panel2_fiyat.scrapers.site_kozaydinlatma.time.sleep"),
        ):
            urls = _koz_sitemap_product_urls("https://example.com/products.xml")
        self.assertEqual(
            urls,
            [
                "https://www.kozaydinlatma.com/urun/test-1",
                "https://www.kozaydinlatma.com/urun/test-2",
            ],
        )


class KrcElektromarketStockTests(unittest.TestCase):
    def test_reads_vat_prices_exact_stock_and_supplier_code(self):
        payload = {"PRODUCTS": [{
            "TITLE": "Gwest 3501 Geçmeli Buat Klemens",
            "IS_ACTIVE": "1",
            "BRAND": "GWEST",
            "SUPPLIER_PRODUCT_CODE": "GWE 3501",
            "PRODUCT_CODE": "FARKLI-KOD",
            "PRICE_SELL": "2.7",
            "PRICE_NOT_DISCOUNTED": 6,
            "VAT": 20,
            "DISPLAY_VAT": "1",
            "STOCK": "550",
            "IN_STOCK": True,
            "CATEGORY_PATH": "ELEKTRİK > Klemensler > ",
            "CATEGORY_NAME": "Buat Klemens",
            "URL": "gwest-3501-gecmeli-buat-klemens",
        }]}
        row = _parse_krcelektromarket_products(
            payload,
            "2026-07-28 12:00:00",
        )[0]
        self.assertEqual(row["brand"], "GWEST")
        self.assertEqual(row["product_code"], "GWE 3501")
        self.assertEqual(row["stock_quantity"], 550)
        self.assertEqual(row["stock"], "Stokta")
        self.assertEqual(row["list_price"], 7.2)
        self.assertEqual(row["price"], 3.24)
        self.assertEqual(row["discount_percent"], 55)
        self.assertEqual(
            row["category"],
            "ELEKTRİK > Klemensler > Buat Klemens",
        )

    def test_negative_stock_and_fallback_price_are_preserved(self):
        payload = {"PRODUCTS": [{
            "TITLE": "Test Ürün",
            "IS_ACTIVE": "1",
            "BRAND": "TEST",
            "PRODUCT_CODE": "TEST-1",
            "PRICE_SELL": "",
            "PRICE_VISITOR": "100",
            "PRICE_NOT_DISCOUNTED": "80",
            "VAT": 20,
            "DISPLAY_VAT": "1",
            "STOCK": "-4.5",
            "IN_STOCK": True,
            "URL": "test-urun",
        }]}
        row = _parse_krcelektromarket_products(
            payload,
            "2026-07-28 12:00:00",
        )[0]
        self.assertEqual(row["stock_quantity"], -4.5)
        self.assertEqual(row["stock"], "Tükendi")
        self.assertEqual(row["price"], 120)
        self.assertEqual(row["list_price"], 120)
        self.assertEqual(row["discount_percent"], 0)


class BuyukalplerStockTests(unittest.TestCase):
    def test_reads_product_model_prices_stock_and_breadcrumbs(self):
        html = """
        <script>
        pageParams = {product: {
          id: "2915",
          sku: "HS-2201",
          fullName: "Helios HS-2201 30W Günışığı AR111 Cob Led Spot",
          categoryName: "Diğer",
          brandName: "Helios",
          priceWithCurrency: 740,
          salePrice: 390.72,
          tax: 20
        }};
        </script>
        <div id="breadcrumbs"><ol>
          <li><a href="/kategori/aydinlatma"><span>Aydınlatma</span></a></li>
          <li><a href="/kategori/armaturler-1"><span>Armatürler</span></a></li>
          <li><a href="/kategori/diger"><span>Diğer</span></a></li>
        </ol></div>
        <input data-stockamount='130'>
        """
        row = _parse_buyukalpler_product(
            html,
            "https://www.buyukalpler.com.tr/urun/test",
            "2026-07-28 13:00:00",
        )
        self.assertEqual(row["brand"], "Helios")
        self.assertEqual(row["product_code"], "HS-2201")
        self.assertEqual(row["stock_quantity"], 130)
        self.assertEqual(row["stock"], "Stokta")
        self.assertEqual(row["list_price"], 888)
        self.assertEqual(row["price"], 390.72)
        self.assertEqual(row["discount_percent"], 56)
        self.assertEqual(
            row["category"],
            "Aydınlatma > Armatürler > Diğer",
        )

    def test_negative_stock_is_preserved_but_unavailable(self):
        html = """
        <script>
        pageParams = {product: {
          sku: "TEST-1",
          fullName: "Test Ürün",
          categoryName: "Test",
          brandName: "Test",
          priceWithCurrency: 100,
          salePrice: 120,
          tax: 20
        }};
        </script>
        <input data-stockamount='-7.5'>
        """
        row = _parse_buyukalpler_product(
            html,
            "https://www.buyukalpler.com.tr/urun/test",
            "2026-07-28 13:00:00",
        )
        self.assertEqual(row["stock_quantity"], -7.5)
        self.assertEqual(row["stock"], "Tükendi")
        self.assertEqual(row["list_price"], 120)
        self.assertEqual(row["price"], 120)
        self.assertEqual(row["discount_percent"], 0)


class TumpaElektrikStockTests(unittest.TestCase):
    def test_reads_all_products_from_next_payload(self):
        products = [{
            "marka": "WAGO",
            "code": "221-413",
            "slug": "wago-221-413-klemens",
            "title": "221-413 Klemens",
            "price": {
                "amount": 16.77497283,
                "tax": 20,
                "discount": 59.5,
                "amountWithoutDiscount": 41.42,
            },
            "stock": {"inStock": True, "amount": 47910},
            "categorySlug": "klemens",
        }]
        payload = f'1:{{"productsProp":{json.dumps(products)}}}'
        frame = json.dumps([1, payload])
        html = f"<script>self.__next_f.push({frame})</script>"

        rows = _parse_tumpaelektrik_products(
            html,
            "Sarf Malzeme",
            "2026-07-27 15:00:00",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["brand"], "WAGO")
        self.assertEqual(rows[0]["product_code"], "221-413")
        self.assertEqual(rows[0]["stock_quantity"], 47910)
        self.assertEqual(rows[0]["stock"], "Stokta")
        self.assertEqual(rows[0]["list_price"], 49.7)
        self.assertEqual(rows[0]["price"], 20.13)
        self.assertEqual(rows[0]["discount_percent"], 59.5)

    def test_negative_stock_is_unavailable(self):
        products = [{
            "marka": "ABB",
            "code": "A-1",
            "slug": "abb-a-1",
            "title": "A-1 Test",
            "price": {"amount": 100, "tax": 20, "discount": 0},
            "stock": {"inStock": True, "amount": -3},
            "categorySlug": "sigorta",
        }]
        payload = f'1:{{"productsProp":{json.dumps(products)}}}'
        html = (
            "<script>self.__next_f.push("
            f"{json.dumps([1, payload])}"
            ")</script>"
        )
        row = _parse_tumpaelektrik_products(
            html,
            "Şalt Malzeme",
            "2026-07-27 15:00:00",
        )[0]
        self.assertEqual(row["stock_quantity"], -3)
        self.assertEqual(row["stock"], "Tükendi")


class ZeybekMarketStockTests(unittest.TestCase):
    def test_reads_vat_inclusive_prices_and_exact_stock(self):
        payload = {"PRODUCTS": [{
            "ID": "8020",
            "TITLE": "Siemens 4*63 Kaçak Akım Koruma Rölesi 30 Ma",
            "BRAND": "SIEMENS",
            "PRODUCT_CODE": "S 5SV5346-6",
            "PRICE_SELL": "2406.1833333333",
            "PRICE_NOT_DISCOUNTED": 7077,
            "VAT": 20,
            "DISPLAY_VAT": "1",
            "DISCOUNT_PERCENT_RAW": 66,
            "STOCK": "300",
            "IN_STOCK": True,
            "CATEGORY_PATH": "Şalt Ürünleri > ",
            "CATEGORY_NAME": "Kaçak Akım Koruma Röleleri",
            "URL": "363-kar-30-ma-yeni-8020",
        }]}
        row = _parse_zeybekmarket_products(
            payload,
            "2026-07-27 16:00:00",
        )[0]
        self.assertEqual(row["brand"], "SIEMENS")
        self.assertEqual(row["stock_quantity"], 300)
        self.assertEqual(row["stock"], "Stokta")
        self.assertEqual(row["list_price"], 8492.4)
        self.assertEqual(row["price"], 2887.42)
        self.assertEqual(row["discount_percent"], 66)
        self.assertEqual(
            row["category"],
            "Şalt Ürünleri > Kaçak Akım Koruma Röleleri",
        )

    def test_negative_stock_is_preserved_but_unavailable(self):
        payload = {"PRODUCTS": [{
            "TITLE": "Test Ürün",
            "BRAND": "TEST",
            "PRICE_SELL": 100,
            "PRICE_NOT_DISCOUNTED": 100,
            "VAT": 20,
            "DISPLAY_VAT": "1",
            "STOCK": "-4.5",
            "IN_STOCK": True,
            "URL": "test-urun",
        }]}
        row = _parse_zeybekmarket_products(
            payload,
            "2026-07-27 16:00:00",
        )[0]
        self.assertEqual(row["stock_quantity"], -4.5)
        self.assertEqual(row["stock"], "Tükendi")


if __name__ == "__main__":
    unittest.main()
