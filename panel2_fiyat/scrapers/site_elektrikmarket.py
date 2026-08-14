"""Elektrikmarket tam katalog fiyat ve stok scraper'ı."""
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

from panel2_fiyat.cancel import ScrapeCancelled
from panel2_fiyat.scrapers import BasePriceScraper, register_scraper
from panel2_fiyat.scrapers._generic import _infer_product_code, _parse_tr_price


BASE_URL = "https://www.elektrikmarket.com.tr"
CATEGORY_WORKERS = 4
DETAIL_WORKERS = 10
REQUEST_TIMEOUT = (10, 30)

# Ana menüdeki birbirini tekrar etmeyen en üst katalog grupları.
CATEGORIES = [
    ("Aydınlatma", "/kategori/aydinlatma"),
    ("Led Aydınlatma", "/kategori/led-aydinlatma"),
    ("Anahtar Priz", "/kategori/anahtar-priz"),
    ("Kablolar", "/kategori/enerji-kablosu"),
    ("Elektronik", "/kategori/elektronik"),
    (
        "Elektrik Tesisat Malzemeleri",
        "/kategori/elektrik-tesisat-malzemeleri",
    ),
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

_SKU_PATTERN = re.compile(r'\bsku:\s*"((?:\\.|[^"])*)"')
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
    raise RuntimeError(f"Elektrikmarket sayfası alınamadı: {url}: {last_error}")


def _page_count(html: str) -> int:
    pages = [
        int(match.group(1))
        for match in re.finditer(r"[?&]tp=(\d+)", html)
    ]
    discovered = max(pages, default=1)
    test_limit = int(os.getenv("ELEKTRIKMARKET_MAX_PAGES", "0") or 0)
    return min(discovered, test_limit) if test_limit > 0 else discovered


def _card_text(card, selector: str) -> str:
    element = card.select_one(selector)
    return element.get_text(" ", strip=True) if element else ""


def _brand(card, product_name: str) -> str:
    brand = _card_text(card, ".showcase-brand")
    if brand:
        return brand
    normalized = product_name.casefold()
    for prefix, fallback in (
        ("3m ", "3M"),
        ("al şerit ", "AL"),
        ("eae ", "EAE"),
        ("kumtel ", "Kumtel"),
        ("legrand ", "Legrand"),
    ):
        if normalized.startswith(prefix.casefold()):
            return fallback
    return "Markasız"


def _parse_category_products(
    html: str,
    category: str,
    scraped_at: str,
) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for card in soup.select("div.showcase"):
        title_link = card.select_one(".showcase-title a[href]")
        if not title_link:
            continue

        name = title_link.get("title") or title_link.get_text(" ", strip=True)
        url = urljoin(BASE_URL, title_link.get("href") or "")
        price = _parse_tr_price(_card_text(card, ".showcase-price-new"))
        list_price = _parse_tr_price(_card_text(card, ".showcase-price-old"))
        if list_price is None:
            list_price = price
        discount = None
        if price is not None and list_price:
            discount = round(max(0, (list_price - price) / list_price * 100), 2)

        rows.append({
            "brand": _brand(card, name),
            "product_code": _infer_product_code(name),
            "product_name": name.strip(),
            "stock": "Stokta" if card.select_one(".add-to-cart-button") else "Tükendi",
            "stock_quantity": None,
            "discount_percent": discount,
            "list_price": list_price,
            "old_price": list_price if list_price != price else None,
            "price": price,
            "currency": "TRY",
            "category": category,
            "url": url,
            "scraped_at": scraped_at,
            "_expects_stock": bool(card.select_one(".add-to-cart-button")),
        })
    return rows


def _decode_js_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def _number(value):
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _detail_values(html: str) -> tuple[str, int | float | None]:
    sku_match = _SKU_PATTERN.search(html)
    stock_match = _STOCK_PATTERN.search(html)
    product_code = (
        _decode_js_string(sku_match.group(1)).strip()
        if sku_match
        else ""
    )
    quantity = _number(stock_match.group(1)) if stock_match else None
    return product_code, quantity


def _fetch_detail(url: str, expects_stock: bool) -> tuple[str, int | float]:
    """Ürün kodu ve gerçek stok adedi bulununca yanıtı erken kapat."""
    last_error = None
    for attempt in range(3):
        try:
            with _session().get(
                url,
                timeout=REQUEST_TIMEOUT,
                stream=True,
            ) as response:
                response.raise_for_status()
                response.encoding = "utf-8"
                buffer = ""
                product_code = ""
                for chunk in response.iter_content(chunk_size=32768):
                    if not chunk:
                        continue
                    buffer += chunk.decode("utf-8", errors="replace")
                    if not product_code:
                        product_code, _ = _detail_values(buffer)
                        if product_code:
                            if not expects_stock:
                                return product_code.strip(), 0
                    if expects_stock:
                        _, quantity = _detail_values(buffer)
                        if quantity is not None:
                            return product_code.strip(), quantity or 0

                    # Aranan ifadelerin bölünen iki parça arasında kalmasını
                    # önleyip belleği sınırlı tut.
                    if len(buffer) > 700_000:
                        buffer = buffer[-350_000:]

                return product_code.strip(), 0
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.0 + attempt)
    raise RuntimeError(f"Elektrikmarket ürün detayı alınamadı: {url}: {last_error}")


@register_scraper("elektrikmarket")
class ElektrikmarketScraper(BasePriceScraper):
    EXPORT_EXCEL = True
    EXCEL_COLUMNS = EXCEL_COLUMNS

    async def scrape(self) -> list[dict]:
        return await asyncio.to_thread(self._scrape_sync)

    def _scrape_sync(self) -> list[dict]:
        scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        first_pages: dict[str, str] = {}
        page_jobs: list[tuple[str, str]] = []

        for category, path in CATEGORIES:
            if self.should_stop():
                raise ScrapeCancelled("Elektrikmarket cekimi durduruldu")
            first_url = urljoin(BASE_URL, path)
            html = _fetch_html(first_url)
            first_pages[category] = html
            for page_no in range(2, _page_count(html) + 1):
                page_jobs.append((category, f"{first_url}?tp={page_no}"))

        all_rows = []
        for category, html in first_pages.items():
            if self.should_stop():
                raise ScrapeCancelled("Elektrikmarket cekimi durduruldu")
            all_rows.extend(
                _parse_category_products(html, category, scraped_at)
            )

        failures = []
        pool = ThreadPoolExecutor(max_workers=CATEGORY_WORKERS)
        try:
            pending_jobs = iter(page_jobs)
            future_map = {}
            for _ in range(CATEGORY_WORKERS):
                try:
                    category, url = next(pending_jobs)
                except StopIteration:
                    break
                future_map[pool.submit(_fetch_html, url)] = (category, url)
            completed = 0
            while future_map:
                if self.should_stop():
                    raise ScrapeCancelled("Elektrikmarket cekimi durduruldu")
                try:
                    future = next(as_completed(list(future_map), timeout=1))
                except TimeoutError:
                    continue
                category, url = future_map.pop(future)
                try:
                    all_rows.extend(
                        _parse_category_products(
                            future.result(), category, scraped_at
                        )
                    )
                except Exception as exc:
                    failures.append(f"{url}: {exc}")
                completed += 1
                if not self.should_stop():
                    try:
                        next_category, next_url = next(pending_jobs)
                    except StopIteration:
                        pass
                    else:
                        future_map[pool.submit(_fetch_html, next_url)] = (
                            next_category,
                            next_url,
                        )
                if completed % 50 == 0 or completed == len(page_jobs):
                    print(
                        f"Elektrikmarket: {completed}/{len(page_jobs)} "
                        "katalog sayfasi islendi.",
                        flush=True,
                    )
        finally:
            pool.shutdown(
                wait=not self.should_stop(),
                cancel_futures=self.should_stop(),
            )

        if failures:
            raise RuntimeError(
                f"Elektrikmarket kategori çekimi eksik kaldı "
                f"({len(failures)} sayfa): {failures[0]}"
            )

        unique = {}
        for row in all_rows:
            unique[row["url"]] = row
        rows = list(unique.values())

        detail_failures = []
        detail_rows = [
            row for row in rows
            if row["_expects_stock"] or not row["product_code"]
        ]
        detail_urls = {row["url"] for row in detail_rows}
        for row in rows:
            if row["url"] not in detail_urls:
                row["stock_quantity"] = 0

        pool = ThreadPoolExecutor(max_workers=DETAIL_WORKERS)
        try:
            pending_rows = iter(detail_rows)
            future_map = {}
            for _ in range(DETAIL_WORKERS):
                try:
                    row = next(pending_rows)
                except StopIteration:
                    break
                future_map[pool.submit(
                    _fetch_detail,
                    row["url"],
                    row["_expects_stock"],
                )] = row
            completed = 0
            while future_map:
                if self.should_stop():
                    raise ScrapeCancelled("Elektrikmarket cekimi durduruldu")
                try:
                    future = next(as_completed(list(future_map), timeout=1))
                except TimeoutError:
                    continue
                row = future_map.pop(future)
                try:
                    product_code, quantity = future.result()
                    if product_code:
                        row["product_code"] = product_code
                    row["stock_quantity"] = quantity
                    row["stock"] = "Stokta" if quantity > 0 else "Tükendi"
                except Exception as exc:
                    detail_failures.append(f"{row['url']}: {exc}")
                completed += 1
                if not self.should_stop():
                    try:
                        next_row = next(pending_rows)
                    except StopIteration:
                        pass
                    else:
                        future_map[pool.submit(
                            _fetch_detail,
                            next_row["url"],
                            next_row["_expects_stock"],
                        )] = next_row
                if completed % 250 == 0 or completed == len(detail_rows):
                    print(
                        f"Elektrikmarket: {completed}/{len(detail_rows)} "
                        "ürün detayı işlendi.",
                        flush=True,
                    )

        finally:
            pool.shutdown(
                wait=not self.should_stop(),
                cancel_futures=self.should_stop(),
            )

        if detail_failures:
            raise RuntimeError(
                f"Elektrikmarket ürün detayları eksik kaldı "
                f"({len(detail_failures)} ürün): {detail_failures[0]}"
            )

        for row in rows:
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
