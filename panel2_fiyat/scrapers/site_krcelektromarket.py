"""KRC Elektromarket tam katalog fiyat ve stok scraper'ı."""
import asyncio
import math
import os
import time
from datetime import datetime
from urllib.parse import urljoin

import requests

from panel2_fiyat.scrapers import BasePriceScraper, register_scraper


PUBLIC_BASE_URL = "https://www.krcelektromarket.com"
SERVICE_URL = f"{PUBLIC_BASE_URL}/srv/service/product/filter"
REQUEST_TIMEOUT = (15, 180)

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


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else round(number, 4)


def _positive_number(*values):
    for value in values:
        number = _number(value)
        if number is not None and number > 0:
            return number
    return None


def _money(value):
    number = _number(value)
    return round(number, 2) if number is not None else None


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() not in {
            "",
            "0",
            "false",
            "hayır",
            "hayir",
        }
    return bool(value)


def _category(product: dict) -> str:
    path = str(product.get("CATEGORY_PATH") or "").strip().strip(">")
    name = str(product.get("CATEGORY_NAME") or "").strip()
    parts = [part.strip() for part in path.split(">") if part.strip()]
    if name and (not parts or parts[-1].casefold() != name.casefold()):
        parts.append(name)
    return " > ".join(parts)


def _product_row(product: dict, scraped_at: str) -> dict:
    vat = _number(product.get("VAT")) or 0
    vat_multiplier = (
        1 + vat / 100 if _truthy(product.get("DISPLAY_VAT", 1)) else 1
    )

    sale_excluding_vat = _positive_number(
        product.get("PRICE_SELL"),
        product.get("PRICE_VISITOR"),
        product.get("PRICE_CREDIT_CART"),
        product.get("PRICE_MONEY_ORDER"),
        product.get("PRICE_SELL_DEFAULT"),
        product.get("PRICE_RAW"),
        product.get("PRICE_GUEST"),
    )
    list_excluding_vat = _positive_number(
        product.get("PRICE_NOT_DISCOUNTED"),
        product.get("PRICE_GUEST"),
    )
    sale_price = (
        _money(sale_excluding_vat * vat_multiplier)
        if sale_excluding_vat is not None
        else None
    )
    source_list_price = (
        _money(list_excluding_vat * vat_multiplier)
        if list_excluding_vat is not None
        else sale_price
    )
    list_price = (
        sale_price
        if (
            sale_price is not None
            and (source_list_price is None or source_list_price < sale_price)
        )
        else source_list_price
    )

    discount = None
    if sale_price is not None and list_price:
        discount = round(max(0, (list_price - sale_price) / list_price * 100), 2)

    stock_quantity = _number(product.get("STOCK"))
    is_in_stock = (
        _truthy(product.get("IN_STOCK"))
        and stock_quantity is not None
        and stock_quantity > 0
    )

    slug = str(product.get("URL") or "").strip("/")
    return {
        "brand": str(product.get("BRAND") or "Markasız").strip(),
        "product_code": str(
            product.get("SUPPLIER_PRODUCT_CODE")
            or product.get("PRODUCT_CODE")
            or ""
        ).strip(),
        "product_name": str(product.get("TITLE") or "").strip(),
        "stock": "Stokta" if is_in_stock else "Tükendi",
        "stock_quantity": stock_quantity,
        "discount_percent": discount,
        "list_price": list_price,
        "old_price": list_price if list_price != sale_price else None,
        "price": sale_price,
        "currency": "TRY",
        "category": _category(product),
        "url": urljoin(PUBLIC_BASE_URL, f"/{slug}"),
        "scraped_at": scraped_at,
    }


def _parse_products(payload: dict, scraped_at: str) -> list[dict]:
    products = payload.get("PRODUCTS") if isinstance(payload, dict) else None
    if not isinstance(products, list):
        raise RuntimeError(
            "KRC Elektromarket ürün listesi servis yanıtında bulunamadı."
        )
    return [
        _product_row(product, scraped_at)
        for product in products
        if (
            isinstance(product, dict)
            and str(product.get("IS_ACTIVE", "1")) == "1"
            and str(product.get("TITLE") or "").strip()
        )
    ]


def _fetch_catalog(page_size: int, max_products: int) -> list[dict]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.7",
        "Accept-Encoding": "gzip, deflate",
    })

    products = []
    max_pages = max(1, math.ceil(max_products / page_size))
    for page in range(1, max_pages + 1):
        last_error = None
        for attempt in range(3):
            try:
                response = session.get(
                    SERVICE_URL,
                    params={"limit": page_size, "pg": page},
                    timeout=REQUEST_TIMEOUT,
                )
                response.raise_for_status()
                payload = response.json()
                page_products = payload.get("PRODUCTS")
                if not isinstance(page_products, list):
                    raise ValueError("PRODUCTS listesi dönmedi.")
                break
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 + attempt)
        else:
            raise RuntimeError(
                f"KRC Elektromarket katalog sayfası {page} alınamadı: "
                f"{last_error}"
            )

        remaining = max_products - len(products)
        products.extend(page_products[:remaining])
        if len(page_products) < page_size or len(products) >= max_products:
            break

    return products


@register_scraper("krcelektromarket")
class KrcElektromarketScraper(BasePriceScraper):
    EXPORT_EXCEL = True
    EXCEL_COLUMNS = EXCEL_COLUMNS

    async def scrape(self) -> list[dict]:
        scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        page_size = max(
            1, int(os.getenv("KRC_ELEKTROMARKET_PAGE_SIZE", "1000") or 1000)
        )
        max_products = max(
            1, int(os.getenv("KRC_ELEKTROMARKET_MAX_PRODUCTS", "200000") or 200000)
        )
        products = await asyncio.to_thread(
            _fetch_catalog,
            page_size,
            max_products,
        )
        rows = _parse_products({"PRODUCTS": products}, scraped_at)

        unique = {}
        for row in rows:
            unique[row["url"]] = row
        rows = list(unique.values())

        if self.brands:
            selected = {brand.casefold() for brand in self.brands}
            rows = [
                row
                for row in rows
                if (row.get("brand") or "").casefold() in selected
            ]
        rows.sort(key=lambda row: (
            (row.get("brand") or "").casefold(),
            (row.get("product_name") or "").casefold(),
        ))
        return rows
