"""Elektrik Malzemesi tam katalog fiyat ve stok scraper'ı."""
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
from panel2_fiyat.scrapers._generic import _parse_tr_price


BASE_URL = "https://www.elektrikmalzemesi.com"
SEARCH_URL = f"{BASE_URL}/catalog/search"
REQUEST_TIMEOUT = (10, 45)
CATEGORY_WORKERS = 3
STOCK_WORKERS = 6

# Ana menüdeki birbirini tekrar etmeyen en üst katalog grupları.
CATEGORIES = [
    ("Aydınlatma", "/kategori/aydinlatma"),
    ("Elektrik Tesisat Malzemeleri", "/kategori/elektrik-tesisat-malzemeleri"),
    ("Elektronik", "/kategori/elektronik"),
    ("Kablolar", "/kategori/kablolar"),
    ("Otomasyon", "/kategori/otomasyon"),
    ("Pano Otomasyon", "/kategori/pano-otomasyon"),
    ("Priz ve Fişler", "/kategori/priz-ve-fisler"),
    ("Şalt Malzemeleri", "/kategori/salt-malzemeleri"),
]

EXCEL_COLUMNS = [
    ("brand", "Marka", 18),
    ("product_code", "Ürün Kodu", 24),
    ("product_name", "Ürün Adı", 52),
    ("stock", "Stok Durumu", 14),
    ("stock_quantity", "Stok Adedi", 14),
    ("discount_percent", "İskonto %", 13),
    ("list_price", "Liste Fiyatı", 16),
    ("price", "Satış Fiyatı", 16),
    ("category", "Kategori", 34),
    ("url", "Ürün Linki", 45),
    ("scraped_at", "Çekim Tarihi", 20),
]

_SHOWCASE_ITEMS_PATTERN = re.compile(
    r"\bvar\s+showcaseItems\s*=\s*(\[[\s\S]*?\])\s*;?\s*</script>",
    re.I,
)
_STOCK_PATTERN = re.compile(
    r"\bdata-stockamount\s*=\s*['\"]([^'\"]+)['\"]",
    re.I,
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
            "Accept-Encoding": "gzip, deflate",
        })
        _thread_local.session = session
    return session


def _fetch_html(url: str) -> str:
    last_error = None
    for attempt in range(4):
        try:
            response = _session().get(url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 429:
                wait = float(response.headers.get("Retry-After") or 3 + attempt * 2)
                time.sleep(min(wait, 15))
                continue
            response.raise_for_status()
            response.encoding = "utf-8"
            return response.text
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(1.5 + attempt)
    raise RuntimeError(f"Elektrik Malzemesi sayfası alınamadı: {url}: {last_error}")


def _page_count(html: str) -> int:
    pages = [
        int(match.group(1))
        for match in re.finditer(r"[?&]tp=(\d+)", html)
    ]
    discovered = max(pages, default=1)
    test_limit = int(os.getenv("ELEKTRIKMALZEMESI_MAX_PAGES", "0") or 0)
    return min(discovered, test_limit) if test_limit > 0 else discovered


def _showcase_items(html: str) -> list[dict]:
    match = _SHOWCASE_ITEMS_PATTERN.search(html)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [item for item in data if isinstance(item, dict)]


def _card_text(card, selector: str) -> str:
    element = card.select_one(selector)
    return element.get_text(" ", strip=True) if element else ""


def _parse_category_products(
    html: str,
    fallback_category: str,
    scraped_at: str,
) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select("div.showcase")
    items = _showcase_items(html)
    if not cards or not items:
        return []

    rows = []
    for card, item in zip(cards, items):
        title_link = card.select_one(".showcase-title a[href]")
        if not title_link:
            continue

        name = str(
            item.get("fullName")
            or title_link.get("title")
            or title_link.get_text(" ", strip=True)
        ).strip()
        price = _parse_tr_price(_card_text(card, ".showcase-price-new"))
        list_price = _parse_tr_price(_card_text(card, ".showcase-price-old"))
        if list_price is None:
            list_price = price
        discount = None
        if price is not None and list_price:
            discount = round(max(0, (list_price - price) / list_price * 100), 2)

        expects_stock = bool(card.select_one(".add-to-cart-button"))
        rows.append({
            "brand": str(item.get("brandName") or "Markasız").strip(),
            "product_code": str(item.get("sku") or "").strip(),
            "product_name": name,
            "stock": "Stokta" if expects_stock else "Tükendi",
            "stock_quantity": None if expects_stock else 0,
            "discount_percent": discount,
            "list_price": list_price,
            "old_price": list_price if list_price != price else None,
            "price": price,
            "currency": "TRY",
            "category": str(
                item.get("categoryName") or fallback_category
            ).strip(),
            "url": urljoin(BASE_URL, title_link.get("href") or ""),
            "scraped_at": scraped_at,
            "_product_id": int(item.get("id") or 0),
            "_expects_stock": expects_stock,
        })
    return rows


def _number(value):
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else round(number, 4)


def _search_stock(product_id: int, query: str) -> int | float | None:
    last_error = None
    for attempt in range(5):
        try:
            response = _session().get(
                SEARCH_URL,
                params={"q": query},
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json",
                },
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 429:
                wait = float(response.headers.get("Retry-After") or 3 + attempt * 2)
                time.sleep(min(wait, 20))
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("Arama servisi liste döndürmedi.")
            for item in payload:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "product"
                    and int(item.get("id") or 0) == product_id
                ):
                    return _number(item.get("stockAmount"))
            return None
        except Exception as exc:
            last_error = exc
            if attempt < 4:
                time.sleep(1.0 + attempt)
    raise RuntimeError(
        f"Elektrik Malzemesi stok servisi alınamadı "
        f"(ürün {product_id}): {last_error}"
    )


def _detail_stock(url: str) -> int | float | None:
    html = _fetch_html(url)
    match = _STOCK_PATTERN.search(html)
    return _number(match.group(1)) if match else None


def _fetch_stock(row: dict) -> int | float:
    # Hızlı mağaza araması ürün kimliğiyle birlikte gerçek stockAmount döndürür.
    for query in (row.get("product_code"), row.get("product_name")):
        query = str(query or "").strip()
        if not query:
            continue
        quantity = _search_stock(row["_product_id"], query)
        if quantity is not None:
            return quantity

    # Arama sonucu bulunamazsa ürün detayındaki data-stockamount güvenli yedektir.
    quantity = _detail_stock(row["url"])
    return quantity if quantity is not None else 0


def _load_stock_cache(path: str) -> dict[int, int | float]:
    cache = {}
    if not path or not os.path.exists(path):
        return cache
    try:
        with open(path, "r", encoding="utf-8") as cache_file:
            for line in cache_file:
                try:
                    item = json.loads(line)
                    product_id = int(item.get("product_id") or 0)
                    quantity = _number(item.get("quantity"))
                    if product_id and quantity is not None:
                        cache[product_id] = quantity
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
    except OSError:
        return {}
    return cache


def _append_stock_cache(path: str, product_id: int, quantity) -> None:
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as cache_file:
        cache_file.write(json.dumps({
            "product_id": product_id,
            "quantity": quantity,
        }, ensure_ascii=False) + "\n")


@register_scraper("elektrikmalzemesi")
class ElektrikMalzemesiScraper(BasePriceScraper):
    EXPORT_EXCEL = True
    EXCEL_COLUMNS = EXCEL_COLUMNS

    async def scrape(self) -> list[dict]:
        return await asyncio.to_thread(self._scrape_sync)

    def _scrape_sync(self) -> list[dict]:
        scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        category_limit = int(
            os.getenv("ELEKTRIKMALZEMESI_MAX_CATEGORIES", "0") or 0
        )
        categories = (
            CATEGORIES[:category_limit] if category_limit > 0 else CATEGORIES
        )

        first_pages: dict[str, str] = {}
        page_jobs: list[tuple[str, str]] = []
        for category, path in categories:
            first_url = urljoin(BASE_URL, path)
            html = _fetch_html(first_url)
            first_pages[category] = html
            for page_no in range(2, _page_count(html) + 1):
                page_jobs.append((category, f"{first_url}?tp={page_no}"))

        all_rows = []
        for category, html in first_pages.items():
            all_rows.extend(
                _parse_category_products(html, category, scraped_at)
            )

        failures = []
        with ThreadPoolExecutor(max_workers=CATEGORY_WORKERS) as pool:
            future_map = {
                pool.submit(_fetch_html, url): (category, url)
                for category, url in page_jobs
            }
            for future in as_completed(future_map):
                category, url = future_map.pop(future)
                try:
                    all_rows.extend(
                        _parse_category_products(
                            future.result(), category, scraped_at
                        )
                    )
                except Exception as exc:
                    failures.append(f"{url}: {exc}")

        if failures:
            raise RuntimeError(
                f"Elektrik Malzemesi kategori çekimi eksik kaldı "
                f"({len(failures)} sayfa): {failures[0]}"
            )

        unique = {}
        for row in all_rows:
            unique[row["url"]] = row
        rows = list(unique.values())

        detail_rows = [row for row in rows if row["_expects_stock"]]
        detail_limit = int(
            os.getenv("ELEKTRIKMALZEMESI_MAX_DETAILS", "0") or 0
        )
        if detail_limit > 0:
            detail_rows = detail_rows[:detail_limit]

        cache_path = os.getenv("ELEKTRIKMALZEMESI_STOCK_CACHE", "").strip()
        stock_cache = _load_stock_cache(cache_path)
        pending_rows = []
        for row in detail_rows:
            product_id = row["_product_id"]
            if product_id in stock_cache:
                quantity = stock_cache[product_id]
                row["stock_quantity"] = quantity
                row["stock"] = "Stokta" if quantity > 0 else "Tükendi"
            else:
                pending_rows.append(row)

        stock_workers = max(
            1,
            int(
                os.getenv(
                    "ELEKTRIKMALZEMESI_STOCK_WORKERS",
                    str(STOCK_WORKERS),
                )
                or STOCK_WORKERS
            ),
        )
        detail_failures = []
        with ThreadPoolExecutor(max_workers=stock_workers) as pool:
            future_map = {
                pool.submit(_fetch_stock, row): row
                for row in pending_rows
            }
            completed = len(detail_rows) - len(pending_rows)
            for future in as_completed(future_map):
                row = future_map.pop(future)
                try:
                    quantity = future.result()
                    row["stock_quantity"] = quantity
                    row["stock"] = "Stokta" if quantity > 0 else "Tükendi"
                    _append_stock_cache(
                        cache_path,
                        row["_product_id"],
                        quantity,
                    )
                except Exception as exc:
                    detail_failures.append(f"{row['url']}: {exc}")
                completed += 1
                if completed % 500 == 0 or completed == len(detail_rows):
                    print(
                        f"Elektrik Malzemesi: {completed}/{len(detail_rows)} "
                        "ürün stoku işlendi.",
                        flush=True,
                    )

        if detail_failures:
            raise RuntimeError(
                f"Elektrik Malzemesi stok çekimi eksik kaldı "
                f"({len(detail_failures)} ürün): {detail_failures[0]}"
            )

        for row in rows:
            row.pop("_product_id", None)
            row.pop("_expects_stock", None)

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
