"""Tümpa Elektrik tam katalog fiyat ve stok scraper'ı."""
import asyncio
import json
import math
import os
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from panel2_fiyat.scrapers import BasePriceScraper, register_scraper
from panel2_fiyat.scrapers._generic import _launch_chromium


BASE_URL = "https://tumpaelektrik.com"
REQUEST_TIMEOUT = (10, 60)

# Ana menüdeki birbirini tekrar etmeyen en üst katalog grupları.
CATEGORIES = [
    ("Şalt Malzeme", "/kategori/salt-malzeme"),
    ("Sarf Malzeme", "/kategori/sarf-malzeme"),
    ("Kablo", "/kategori/kablo"),
    ("Aydınlatma", "/kategori/aydinlatma"),
    ("Anahtar ve Priz", "/kategori/anahtar-ve-priz"),
    ("Elektrik ve Elektronik", "/kategori/elektrik-ve-elektronik"),
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
    ("category", "Kategori", 28),
    ("url", "Ürün Linki", 45),
    ("scraped_at", "Çekim Tarihi", 20),
]


def _session() -> requests.Session:
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
    return session


def _fetch_html(session: requests.Session, url: str) -> str:
    last_error = None
    for attempt in range(3):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            response.encoding = "utf-8"
            return response.text
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.0 + attempt)
    raise RuntimeError(f"Tümpa Elektrik sayfası alınamadı: {url}: {last_error}")


def _flight_payloads(html: str):
    """Next.js Flight scriptlerinin JSON metinlerini döndür."""
    soup = BeautifulSoup(html, "lxml")
    prefix = "self.__next_f.push("
    for script in soup.select("script"):
        source = script.string or script.get_text()
        if not source:
            continue
        source = source.strip()
        if not source.startswith(prefix) or not source.endswith(")"):
            continue
        try:
            frame = json.loads(source[len(prefix):-1])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            isinstance(frame, list)
            and len(frame) > 1
            and isinstance(frame[1], str)
        ):
            yield frame[1]


def _products_from_html(html: str) -> list[dict]:
    """Kategori sayfasındaki eksiksiz ``productsProp`` dizisini çöz."""
    marker = '"productsProp":'
    decoder = json.JSONDecoder()
    largest = []
    for payload in _flight_payloads(html):
        search_from = 0
        while True:
            start = payload.find(marker, search_from)
            if start < 0:
                break
            start += len(marker)
            search_from = start
            try:
                products, end = decoder.raw_decode(payload, start)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            search_from = end
            if isinstance(products, list):
                valid = [
                    product for product in products
                    if isinstance(product, dict)
                ]
                if len(valid) > len(largest):
                    largest = valid
    if largest:
        return largest
    raise RuntimeError("Tümpa Elektrik productsProp verisi bulunamadı.")


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else round(number, 4)


def _money(value):
    number = _number(value)
    return round(number, 2) if number is not None else None


def _product_row(product: dict, category: str, scraped_at: str) -> dict:
    price_info = product.get("price") or {}
    tax_rate = _number(price_info.get("tax"))
    tax_multiplier = 1 + ((tax_rate if tax_rate is not None else 20) / 100)

    sale_excluding_tax = _number(price_info.get("amount"))
    list_excluding_tax = _number(price_info.get("amountWithoutDiscount"))
    sale_price = (
        _money(sale_excluding_tax * tax_multiplier)
        if sale_excluding_tax is not None
        else None
    )
    list_price = (
        _money(list_excluding_tax * tax_multiplier)
        if list_excluding_tax is not None
        else sale_price
    )
    if list_price is None:
        list_price = sale_price

    discount = _number(price_info.get("discount"))
    if discount is None and sale_price is not None and list_price:
        discount = round(max(0, (list_price - sale_price) / list_price * 100), 2)

    stock_info = product.get("stock") or {}
    stock_quantity = _number(stock_info.get("amount"))
    is_in_stock = bool(stock_info.get("inStock")) and (
        stock_quantity is not None and stock_quantity > 0
    )

    category_slug = str(product.get("categorySlug") or "").strip("/")
    product_slug = str(product.get("slug") or "").strip("/")
    product_url = urljoin(BASE_URL, f"/{category_slug}/{product_slug}")

    return {
        "brand": str(product.get("marka") or "Markasız").strip(),
        "product_code": str(product.get("code") or "").strip(),
        "product_name": str(product.get("title") or "").strip(),
        "stock": "Stokta" if is_in_stock else "Tükendi",
        "stock_quantity": stock_quantity,
        "discount_percent": discount,
        "list_price": list_price,
        "old_price": list_price if list_price != sale_price else None,
        "price": sale_price,
        "currency": "TRY",
        "category": category,
        "url": product_url,
        "scraped_at": scraped_at,
    }


def _parse_products(html: str, category: str, scraped_at: str) -> list[dict]:
    return [
        _product_row(product, category, scraped_at)
        for product in _products_from_html(html)
    ]


async def _action_page_products(
    page,
    category_slug: str,
    page_no: int,
    action: str,
) -> list[dict]:
    products = await page.evaluate(
        """async ({url, action, args}) => {
            const response = await fetch(url, {
                method: "POST",
                headers: {
                    "Accept": "text/x-component",
                    "Content-Type": "text/plain;charset=UTF-8",
                    "Next-Action": action,
                },
                body: JSON.stringify(args),
            });
            const text = await response.text();
            const line = text.split("\\n").find(
                value => value.startsWith("1:")
            );
            if (!line) {
                throw new Error(
                    `Tümpa sayfa verisi alınamadı: ${text.slice(0, 500)}`
                );
            }
            return JSON.parse(line.slice(2));
        }""",
        {
            "url": (
                f"{BASE_URL}/kategori/{category_slug}?page={page_no}"
            ),
            "action": action,
            "args": [
                "",
                "",
                category_slug,
                "$undefined",
                "$undefined",
                page_no - 1,
                20,
                [],
                {"inStock": False},
                [],
                [],
                0,
                None,
                "",
            ],
        },
    )
    if not isinstance(products, list):
        raise RuntimeError(
            f"Tümpa Elektrik {category_slug} sayfa {page_no} "
            "ürün dizisi alınamadı."
        )
    return products


async def _scrape_category_pages(page, path: str) -> list[dict]:
    url = urljoin(BASE_URL, path)
    await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    await page.wait_for_selector(".productItem", timeout=30_000)
    products = _products_from_html(await page.content())
    if not products:
        return []

    total = int(_number(products[0].get("count")) or len(products))
    page_count = math.ceil(total / 20)
    test_limit = int(os.getenv("TUMPA_MAX_PAGES", "0") or 0)
    if test_limit > 0:
        page_count = min(page_count, test_limit)
    if page_count <= 1:
        return products

    category_slug = path.rstrip("/").rsplit("/", 1)[-1]
    category_token = json.dumps(category_slug, ensure_ascii=False)

    def is_product_request(request):
        body = request.post_data or ""
        return (
            request.method == "POST"
            and category_token in body
            and ",1,20," in body
        )

    async with page.expect_request(is_product_request, timeout=90_000) as info:
        await page.locator('a[title="2"]').click()
    action_request = await info.value
    action = action_request.headers.get("next-action")
    if not action:
        raise RuntimeError("Tümpa Elektrik sayfalama anahtarı bulunamadı.")

    for page_no in range(2, page_count + 1):
        products.extend(
            await _action_page_products(
                page,
                category_slug,
                page_no,
                action,
            )
        )
    return products


@register_scraper("tumpaelektrik")
class TumpaScraper(BasePriceScraper):
    EXPORT_EXCEL = True
    EXCEL_COLUMNS = EXCEL_COLUMNS

    async def scrape(self) -> list[dict]:
        scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        category_limit = int(os.getenv("TUMPA_MAX_CATEGORIES", "0") or 0)
        categories = CATEGORIES[:category_limit] if category_limit > 0 else CATEGORIES

        async with async_playwright() as playwright:
            browser = await _launch_chromium(playwright, headless=True)
            try:
                context = await browser.new_context(locale="tr-TR")
                page = await context.new_page()

                async def block_heavy_assets(route):
                    if route.request.resource_type in {"image", "font", "media"}:
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/*", block_heavy_assets)
                for category, path in categories:
                    products = await _scrape_category_pages(page, path)
                    category_rows = [
                        _product_row(product, category, scraped_at)
                        for product in products
                    ]
                    rows.extend(category_rows)
                    print(
                        f"Tümpa Elektrik: {category} - "
                        f"{len(category_rows)} ürün.",
                        flush=True,
                    )
            finally:
                await browser.close()

        unique = {}
        for row in rows:
            unique[row["url"]] = row
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
