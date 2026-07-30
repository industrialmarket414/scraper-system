"""Elektrix tam katalog fiyat ve stok scraper'ı."""
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

from panel2_fiyat.scrapers import BasePriceScraper, register_scraper


BASE_URL = "https://www.elektrix.com"
MAX_WORKERS = 4
REQUEST_TIMEOUT = (10, 25)

# Ana menüdeki birbirini tekrar etmeyen en üst katalog grupları.
CATEGORIES = [
    ("Aydınlatma", "/aydinlatma"),
    ("Anahtar ve Priz", "/anahtar-ve-priz"),
    ("Şalt Malzemeleri", "/salt"),
    ("Güç Kaynağı", "/guc-kaynagi"),
    ("Güvenlik", "/guvenlik-cihazlari"),
    ("Ev Elektriği", "/ev-elektrigi"),
    ("Sarf", "/sarf"),
    ("Otomasyon", "/otomasyon"),
    ("Kablo", "/kablo"),
    ("Kablo Kanalı", "/kablo-kanali"),
]

EXCEL_COLUMNS = [
    ("brand", "Marka", 18),
    ("product_code", "Ürün Kodu", 24),
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

_PRODUCT_PATTERN = re.compile(
    r"PRODUCT_DATA\.push\(JSON\.parse\('((?:\\.|[^'])*)'\)\)"
)
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
            "Connection": "close",
        })
        _thread_local.session = session
    return session


def _fetch_html(url: str, allow_not_found: bool = False) -> str | None:
    last_error = None
    for attempt in range(3):
        try:
            response = _session().get(url, timeout=REQUEST_TIMEOUT)
            if allow_not_found and response.status_code == 404:
                return None
            response.raise_for_status()
            response.encoding = "utf-8"
            return response.text
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.0 + attempt)
    raise RuntimeError(f"Elektrix sayfası alınamadı: {url}: {last_error}")


def _product_data(html: str | None) -> list[dict]:
    products = []
    for match in _PRODUCT_PATTERN.finditer(html or ""):
        try:
            escaped_json = match.group(1).replace("\\'", "'")
            json_text = json.loads(f'"{escaped_json}"')
            data = json.loads(json_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if data.get("id") and data.get("name"):
            products.append(data)
    return products


def _has_products(url: str, page_no: int) -> bool:
    html = _fetch_html(f"{url}?pg={page_no}", allow_not_found=True)
    return bool(_product_data(html))


def _page_count(url: str) -> int:
    """Sayfa sayısı HTML'de verilmediği için son dolu sayfayı ikili aramayla bul."""
    test_limit = int(os.getenv("ELEKTRIX_MAX_PAGES", "0") or 0)
    if test_limit > 0:
        return test_limit

    upper = 2
    lower = 1
    while _has_products(url, upper):
        lower = upper
        upper *= 2

    while lower + 1 < upper:
        middle = (lower + upper) // 2
        if _has_products(url, middle):
            lower = middle
        else:
            upper = middle

    return lower


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _brand(data: dict) -> str:
    brand = str(data.get("brand") or "").strip()
    if brand:
        return brand

    code = str(data.get("supplier_code") or data.get("code") or "").upper()
    name = str(data.get("name") or "").strip()
    if code.startswith("SCHMT-"):
        return "Schneider Electric"
    for prefix, fallback in (
        ("AYKA ", "Ayka"),
        ("MAPLED ", "Mapled"),
        ("WEICON TOOLS ", "Weicon Tools"),
    ):
        if name.upper().startswith(prefix):
            return fallback
    return ""


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
            "brand": _brand(data),
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


@register_scraper("elektrix")
class ElektrixScraper(BasePriceScraper):
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
            for page_no in range(2, _page_count(first_url) + 1):
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
                        f"Elektrix: {completed}/{len(jobs)} "
                        "ek katalog sayfası işlendi.",
                        flush=True,
                    )

        if failures:
            raise RuntimeError(
                f"Elektrix tam katalog çekimi eksik kaldı "
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
