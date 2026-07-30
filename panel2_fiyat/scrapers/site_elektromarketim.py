"""Elektromarketim tam katalog fiyat ve stok scraper'ı."""
import asyncio
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from panel2_fiyat.scrapers import BasePriceScraper, register_scraper


BASE_URL = "https://www.elektromarketim.com"
MAX_WORKERS = 4
REQUEST_TIMEOUT = (10, 25)

# Ana menüdeki birbirini tekrar etmeyen en üst katalog grupları.
CATEGORIES = [
    ("Aydınlatma", "/c/aydinlatma"),
    ("Otomasyon ve Şalt Malzemeleri", "/c/otomasyon-ve-salt-malzemeleri"),
    ("Elektrik ve Elektronik", "/c/elektrik-ve-elektronik"),
    (
        "Elektrikli Araç Şarj İstasyonları ve Ekipmanları",
        "/c/elektrikli-arac-sarj-istasyonlari-ve-ekipmanlari",
    ),
    ("Kablolar", "/c/kablolar"),
    ("Makineler ve El Aletleri", "/c/makineler-ve-el-aletleri"),
    ("Test ve Ölçü Aletleri", "/c/test-ve-olcu-aletleri"),
    ("Matkaplar", "/c/matkaplar"),
    (
        "Kaldırma ve İş Güvenliği Ekipmanları",
        "/c/kaldirma-ve-is-guvenligi-ekipmanlari",
    ),
    ("Tamirat ve Yapı Malzemeleri", "/c/tamirat-ve-yapi-malzemeleri"),
    (
        "Isıtma, Soğutma ve Havalandırma",
        "/c/isitma-sogutma-ve-havalandirma",
    ),
    ("Bahçe", "/c/bahce"),
]

EXCEL_COLUMNS = [
    ("brand", "Marka", 18),
    ("product_code", "Ürün Kodu", 22),
    ("product_name", "Ürün Adı", 52),
    ("stock", "Stok Durumu", 14),
    ("stock_quantity", "Stok Adedi", 12),
    ("discount_percent", "İskonto %", 13),
    ("list_price", "Liste Fiyatı", 16),
    ("price", "Satış Fiyatı", 16),
    ("category", "Kategori", 34),
    ("url", "Ürün Linki", 45),
    ("scraped_at", "Çekim Tarihi", 20),
]

_thread_local = threading.local()


def _session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.7",
            "Accept-Encoding": "gzip",
            # Bu site uzun süre açık tutulan bağlantıları zaman zaman askıda
            # bırakıyor. Her sayfada bağlantıyı kapatmak tam katalog taramasını
            # öngörülebilir sürede tamamlıyor.
            "Connection": "close",
        })
        _thread_local.session = session
    return session


def _fetch_html(url: str) -> str:
    last_error = None
    for attempt in range(3):
        try:
            response = _session().get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            response.encoding = "utf-8"
            return response.text
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.0 + attempt)
    raise RuntimeError(
        f"Elektromarketim sayfası alınamadı: {url}: {last_error}"
    )


def _page_count(html: str) -> int:
    soup = BeautifulSoup(html, "lxml")
    pages = [1]
    for link in soup.select('a[href*="pg="]'):
        match = re.search(r"[?&]pg=(\d+)", link.get("href") or "")
        if match:
            pages.append(int(match.group(1)))
    discovered = max(pages)
    test_limit = int(os.getenv("ELEKTROMARKETIM_MAX_PAGES", "0") or 0)
    return min(discovered, test_limit) if test_limit > 0 else discovered


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _product_data(html: str) -> list[dict]:
    """Kategori kaynağındaki PRODUCT_DATA kayıtlarının tamamını çıkar."""
    products = []
    pattern = re.compile(
        r"PRODUCT_DATA\.push\(JSON\.parse\('((?:\\.|[^'])*)'\)\)"
    )
    for match in pattern.finditer(html):
        try:
            escaped_json = match.group(1).replace("\\'", "'")
            json_text = json.loads(f'"{escaped_json}"')
            data = json.loads(json_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if data.get("id") and data.get("name"):
            products.append(data)
    return products


def _parse_products(
    html: str,
    fallback_category: str,
    scraped_at: str,
) -> list[dict]:
    rows = []
    for data in _product_data(html):
        quantity = _number(data.get("quantity"))
        price = _number(data.get("total_sale_price"))
        list_price = _number(data.get("total_base_price"))
        if list_price is None:
            list_price = price

        discount = None
        if price is not None and list_price:
            discount = round(max(0, (list_price - price) / list_price * 100), 2)

        category_parts = [
            str(data.get("category_path") or "").strip(" >"),
            str(data.get("category") or "").strip(),
        ]
        category = " > ".join(part for part in category_parts if part)
        url = urljoin(BASE_URL + "/", str(data.get("url") or ""))

        rows.append({
            "brand": str(data.get("brand") or "").strip(),
            "product_code": str(
                data.get("supplier_code") or data.get("code") or ""
            ).strip(),
            "product_name": str(data.get("name") or "").strip(),
            "stock": (
                "Stokta"
                if quantity is not None and quantity > 0
                else "Tükendi"
            ),
            "stock_quantity": quantity,
            "discount_percent": discount,
            "list_price": list_price,
            "old_price": list_price if list_price != price else None,
            "price": price,
            "currency": "TRY",
            "category": category or fallback_category,
            "url": url,
            "scraped_at": scraped_at,
        })
    return rows


@register_scraper("elektromarketim")
class ElektromarketimScraper(BasePriceScraper):
    EXPORT_EXCEL = True
    EXCEL_COLUMNS = EXCEL_COLUMNS

    async def scrape(self) -> list[dict]:
        return await asyncio.to_thread(self._scrape_sync)

    def _scrape_sync(self) -> list[dict]:
        scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        first_pages: dict[str, str] = {}
        jobs: list[tuple[str, str]] = []

        for category, path in CATEGORIES:
            first_url = urljoin(BASE_URL, path)
            html = _fetch_html(first_url)
            first_pages[category] = html
            for page_no in range(2, _page_count(html) + 1):
                jobs.append((category, f"{first_url}?pg={page_no}"))

        all_rows = []
        for category, html in first_pages.items():
            all_rows.extend(_parse_products(html, category, scraped_at))

        failures = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            future_map = {
                pool.submit(_fetch_html, url): (category, url)
                for category, url in jobs
            }
            completed = 0
            for future in as_completed(future_map):
                category, url = future_map.pop(future)
                try:
                    all_rows.extend(
                        _parse_products(future.result(), category, scraped_at)
                    )
                except Exception as exc:
                    failures.append(f"{url}: {exc}")
                completed += 1
                if completed % 50 == 0 or completed == len(jobs):
                    print(
                        "Elektromarketim: "
                        f"{completed}/{len(jobs)} ek katalog sayfası işlendi.",
                        flush=True,
                    )

        if failures:
            raise RuntimeError(
                f"Elektromarketim tam katalog çekimi eksik kaldı "
                f"({len(failures)} sayfa): {failures[0]}"
            )

        unique = {}
        for row in all_rows:
            key = row.get("url") or (
                row.get("brand"),
                row.get("product_code"),
                row.get("product_name"),
            )
            unique[key] = row

        rows = list(unique.values())
        if self.brands:
            selected = {brand.casefold() for brand in self.brands}
            rows = [
                row for row in rows
                if (row.get("brand") or "").casefold() in selected
            ]
        rows.sort(key=lambda row: (
            (row.get("brand") or "").casefold(),
            (row.get("product_name") or "").casefold(),
        ))
        return rows
