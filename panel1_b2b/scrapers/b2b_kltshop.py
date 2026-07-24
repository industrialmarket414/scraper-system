"""
KLTShop B2B Scraper
URL    : https://www.kltshop.com
Giriş  : Gerekmiyor — ürün detayları herkese açık
Çekim  : Ayarlardaki marka listesindeki her marka, tüm ürünleri (requests + BeautifulSoup)
Sütunlar: Ürün Kodu | Ürün Adı | İndirim % | Stok Adeti | Güncel Fiyat | Eski Fiyat | Stok Durumu
"""
import asyncio
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from panel1_b2b.scrapers import BaseB2BScraper, register_scraper

BASE_URL = "https://www.kltshop.com"
HEADERS  = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# Ayarlardan marka gelmezse kullanılacak varsayılan liste
DEFAULT_BRANDS = ["schneider-electric"]


@register_scraper("kltshop")
class KltshopScraper(BaseB2BScraper):
    COLUMNS = [
        ("product_code",  "Ürün Kodu",     20),
        ("product_name",  "Ürün Adı",      40),
        ("discount_text", "İndirim %",     12),
        ("stock_qty",     "Stok Adeti",    15),
        ("min_package_qty", "Min. Paket Adedi", 18),
        ("price_text",    "Güncel Fiyat",  16),
        ("old_price",     "Eski Fiyat",    16),
        ("stock_status",  "Stok Durumu",   20),
    ]

    async def scrape(self) -> list[dict]:
        brands = self.brands if self.brands else DEFAULT_BRANDS
        # requests senkron → asyncio.to_thread ile event loop'u bloklamaz
        return await asyncio.to_thread(self._scrape_sync, brands)

    # ── Senkron çekim (thread içinde çalışır) ────────────────────────────────
    def _scrape_sync(self, brands: list[str]) -> list[dict]:
        products = []
        for brand in brands:
            catalog_url = f"{BASE_URL}/{brand}?ps=999"
            links = _get_product_links(catalog_url)
            for link in links:
                detail = _extract_product(link)
                if detail:
                    products.append(detail)
                time.sleep(0.5)  # sunucuyu aşırı yüklememe için (orijinal koddan)
        return products


# ── Katalog: ürün linklerini al ───────────────────────────────────────────────
def _get_product_links(catalog_url: str) -> list[str]:
    try:
        resp = requests.get(catalog_url, headers=HEADERS, timeout=15)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.content, "html.parser")

        catalog_div = soup.find("div", {"id": "catalog394"})
        if not catalog_div:
            return []

        items = catalog_div.find_all("div", {"class": "product-item"})
        links = []
        for item in items:
            a = item.find("a", href=True)
            if a and a["href"]:
                links.append(urljoin(BASE_URL, a["href"]))
        return links
    except Exception:
        return []


# ── Detay sayfası: ürün bilgilerini çıkar ────────────────────────────────────
def _extract_product(url: str) -> dict | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.content, "html.parser")

        def norm(el):
            return " ".join(el.get_text().split()).strip(":").strip() if el else ""

        # Ürün kodu
        product_code = norm(soup.find("span", {"id": "supplier-product-code"}))

        # Ürün adı
        product_name = norm(soup.find("h1", {"id": "product-title"}))

        # İndirim
        discount = norm(soup.find("span", {"class": "product-discount"})) or "0"

        # Stok adeti
        stock_qty = ""
        code_section = soup.find("div", {"class": "product-code"})
        if code_section:
            stok_txt = code_section.find(
                string=lambda t: t and "Stok Adeti" in t
            )
            if stok_txt:
                sp = stok_txt.parent.find("span")
                if sp:
                    stock_qty = norm(sp)

        # Min. Paket Adedi — ana ürünün miktar kutusundaki min/adım bilgisi
        min_package_qty = ""
        qty_input = soup.select_one(
            ".product-add-buttons input[type='number'][id^='ProductCount']"
        )
        if qty_input:
            values = []
            qty_wrapper = qty_input.find_parent(attrs={"data-increment": True})
            raw_values = [
                qty_wrapper.get("data-increment") if qty_wrapper else None,
                qty_input.get("min"),
                qty_input.get("step"),
            ]
            for raw in raw_values:
                try:
                    value = float(str(raw).replace(",", "."))
                    if value > 0:
                        values.append(value)
                except (TypeError, ValueError):
                    pass
            if values:
                value = max(values)
                min_package_qty = str(int(value)) if value.is_integer() else str(value)

        # Güncel fiyat (KDV hariç)
        price = norm(soup.find("span", {"class": "product-price-not-vat"}))

        # Eski fiyat
        old_price = norm(soup.find("span", {"class": "product-price-not-discounted-not-vat"}))

        # Stok durumu
        stock_status_el = soup.find("div", {"class": "stock-status"})
        stock_status = norm(stock_status_el) if stock_status_el else ""

        if not product_code and not product_name:
            return None

        return {
            "product_code":  product_code,
            "product_name":  product_name,
            "discount_text": discount,
            "stock_qty":     stock_qty,
            "min_package_qty": min_package_qty,
            "price_text":    price,
            "old_price":     old_price,
            "stock_status":  stock_status,
        }
    except Exception:
        return None
