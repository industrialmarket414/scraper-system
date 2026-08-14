"""Elektrikçiden tam katalog fiyat ve stok scraper'ı."""
import asyncio
import os
import time
from datetime import datetime
from urllib.parse import urljoin

import requests

from panel2_fiyat.cancel import ScrapeCancelled
from panel2_fiyat.scrapers import BasePriceScraper, register_scraper


PUBLIC_BASE_URL = "https://www.elektrikciden.com"
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
    vat_multiplier = 1 + vat / 100 if _truthy(product.get("DISPLAY_VAT", 1)) else 1

    sale_excluding_vat = _number(
        product.get("PRICE_SELL")
        if product.get("PRICE_SELL") not in (None, "")
        else product.get("PRICE_RAW")
    )
    list_excluding_vat = _number(product.get("PRICE_NOT_DISCOUNTED"))
    sale_price = (
        _money(sale_excluding_vat * vat_multiplier)
        if sale_excluding_vat is not None
        else None
    )
    list_price = (
        _money(list_excluding_vat * vat_multiplier)
        if list_excluding_vat is not None and list_excluding_vat > 0
        else sale_price
    )

    discount = _number(product.get("DISCOUNT_PERCENT_RAW"))
    if discount is None:
        discount = _number(product.get("DISCOUNT_PERCENT"))
    if discount is None and sale_price is not None and list_price:
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
            product.get("PRODUCT_CODE")
            or product.get("SUPPLIER_PRODUCT_CODE")
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
            "Elektrikçiden ürün listesi servis yanıtında bulunamadı."
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


def _fetch_catalog(limit: int) -> dict:
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
    last_error = None
    for attempt in range(3):
        try:
            response = session.get(
                SERVICE_URL,
                params={"limit": limit},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("JSON nesnesi dönmedi.")
            return payload
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 + attempt)
    raise RuntimeError(f"Elektrikçiden katalog servisi alınamadı: {last_error}")


@register_scraper("elektrikciden")
class ElektrikcidenScraper(BasePriceScraper):
    EXPORT_EXCEL = True
    EXCEL_COLUMNS = EXCEL_COLUMNS

    async def scrape(self) -> list[dict]:
        scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        limit = max(
            1,
            int(os.getenv("ELEKTRIKCIDEN_MAX_PRODUCTS", "10000") or 10000),
        )
        if self.should_stop():
            raise ScrapeCancelled("Elektrikciden cekimi durduruldu")
        print("Elektrikciden: katalog verisi aliniyor.", flush=True)
        payload = await asyncio.to_thread(_fetch_catalog, limit)
        if self.should_stop():
            raise ScrapeCancelled("Elektrikciden cekimi durduruldu")
        rows = _parse_products(payload, scraped_at)
        for completed in range(250, len(rows) + 250, 250):
            if self.should_stop():
                raise ScrapeCancelled("Elektrikciden cekimi durduruldu")
            shown = min(completed, len(rows))
            print(
                f"Elektrikciden: {shown}/{len(rows)} urun hazirlandi.",
                flush=True,
            )
            if shown == len(rows):
                break

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
