"""
Star Akım tam katalog scraper'ı.

Tüm ana ürün gruplarını ve sayfalarını tarar. Her ürün için marka, ürün/model
kodu, stok durumu, liste fiyatı, iskonto ve satış fiyatı döndürür.
"""
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
from panel2_fiyat.scrapers._generic import _infer_product_code, _parse_tr_price


BASE_URL = "https://www.starakim.com"
MAX_WORKERS = 5
DETAIL_WORKERS = 12
REQUEST_TIMEOUT = 30

# Birbirini tekrar etmeyen en üst katalog grupları. Böylece tüm markalar gelir.
CATEGORIES = [
    ("Aydınlatma", "/aydinlatma"),
    ("Anahtar Priz", "/anahtar-priz"),
    ("Enerji Kabloları", "/enerji-kablolari"),
    ("Zayıf Akım Kabloları", "/zayif-akim-kablolari"),
    ("Şalt Ürünleri", "/salt-malzemeler"),
    ("Elektrik Tesisat Malzemeleri", "/elektrik-tesisat-malzemeleri"),
    ("Grup Priz ve Uzatma Kablosu", "/grup-priz-ve-uzatma-kablosu"),
    ("Diafon ve Güvenlik", "/diafon-sistemleri"),
    ("Uydu ve Ses/Görüntü", "/uydu-sistemleri"),
    ("Havalandırma ve Isıtma", "/havalandirma-ve-isitma"),
]

# Katalog kartında eski/kırık adresi kalan ürünler için güncel ürün adresi.
DETAIL_URL_ALIASES = {
    (
        "https://www.starakim.com/"
        "xlpe-a2xsy--yaxc7v-r--20/35-kv-1x95/16"
    ): (
        "https://www.starakim.com/"
        "xlpe-2xsy--yxc7v-r--20/35-kv-1x95/16"
    ),
}

EXCEL_COLUMNS = [
    ("brand", "Marka", 18),
    ("product_code", "Ürün Kodu", 20),
    ("product_name", "Ürün Adı", 52),
    ("stock", "Stok Durumu", 14),
    ("stock_quantity", "Stok Adedi", 12),
    ("discount_percent", "İskonto %", 13),
    ("list_price", "Liste Fiyatı", 16),
    ("price", "Satış Fiyatı", 16),
    ("category", "Kategori", 25),
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
        })
        _thread_local.session = session
    return session


def _fetch_html(url: str) -> str:
    last_error = None
    for attempt in range(3):
        try:
            response = _session().get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or "utf-8"
            return response.text
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.0 + attempt)
    raise RuntimeError(f"Star Akım sayfası alınamadı: {url}: {last_error}")


def _page_count(html: str) -> int:
    soup = BeautifulSoup(html, "lxml")
    pages = [1]
    for link in soup.select('a[href*="pg="]'):
        href = link.get("href") or ""
        match = re.search(r"[?&]pg=(\d+)", href)
        if match:
            pages.append(int(match.group(1)))
    discovered = max(pages)
    test_limit = int(os.getenv("STARAKIM_MAX_PAGES", "0") or 0)
    return min(discovered, test_limit) if test_limit > 0 else discovered


def _text(card, selector: str) -> str:
    element = card.select_one(selector)
    return element.get_text(" ", strip=True) if element else ""


def _product_code(name: str, brand: str) -> str:
    code = _infer_product_code(name)
    if code:
        return code
    remainder = name
    if brand and name.casefold().startswith(brand.casefold()):
        remainder = name[len(brand):].strip()
    match = re.match(r"(\d{4,12})(?:\s+([A-Za-z]+))?", remainder)
    if not match:
        return ""
    possible_unit = (match.group(2) or "").upper()
    if possible_unit in {"W", "V", "A", "K", "MM", "CM", "MT", "HZ", "LM"}:
        return ""
    return match.group(1)


def _parse_products(html: str, category: str, scraped_at: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    products = []
    for card in soup.select("div.product-item"):
        name_element = card.select_one(".product-title")
        if not name_element:
            continue
        name = name_element.get_text(" ", strip=True)
        href = name_element.get("href") or ""
        url = urljoin(BASE_URL, href)

        price = _parse_tr_price(_text(card, ".current-price .product-price"))
        old_price = _parse_tr_price(
            _text(card, ".product-price-not-discounted")
        )
        list_price = old_price or price
        discount_text = _text(card, ".product-discount")
        try:
            discount = float(discount_text.replace(",", ".")) if discount_text else None
        except ValueError:
            discount = None
        if discount is None and price is not None and list_price and list_price > price:
            discount = round((list_price - price) / list_price * 100, 2)
        if discount is None and price is not None:
            discount = 0.0

        # Star Akım sayısal adet yayınlamıyor. Sepete ekleme düğmesi stok
        # göstergesidir; fiyatı/sepete eklemesi olmayan kart tükenmiş sayılır.
        in_stock = bool(card.select_one(".add-to-cart-btn")) and price is not None
        brand = _text(card, ".brand-title") or name.split(maxsplit=1)[0]
        products.append({
            "brand": brand,
            "product_code": _product_code(name, brand),
            "product_name": name,
            "stock": "Stokta" if in_stock else "Tükendi",
            "stock_quantity": None,
            "discount_percent": discount,
            "list_price": list_price,
            "old_price": old_price,
            "price": price,
            "currency": "TRY",
            "category": category,
            "url": url,
            "scraped_at": scraped_at,
        })
    return products


def _detail_data(html: str) -> dict:
    """Ürün sayfasındaki PRODUCT_DATA kaydından gerçek stok ve ürün kodunu al."""
    soup = BeautifulSoup(html, "lxml")
    current_id = ""
    product_id = soup.select_one("#product-id")
    if product_id:
        current_id = str(product_id.get("value") or "")

    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if "PRODUCT_DATA.push" not in text:
            continue
        for match in re.finditer(
            r"PRODUCT_DATA\.push\(JSON\.parse\('((?:\\.|[^'])*)'\)\)",
            text,
        ):
            try:
                # İçerik, JavaScript içinde kaçışlanmış bir JSON metnidir.
                # Tek tırnaklı JS metnindeki \' kaçışı JSON standardında geçerli
                # değildir; dış JSON dizesine çevirmeden önce normalleştir.
                escaped_json = match.group(1).replace("\\'", "'")
                json_text = json.loads(f'"{escaped_json}"')
                data = json.loads(json_text)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not current_id or str(data.get("id") or "") == current_id:
                return data
    return {}


def _enrich_product_detail(row: dict) -> dict:
    detail_url = DETAIL_URL_ALIASES.get(row["url"], row["url"])
    detail = _detail_data(_fetch_html(detail_url))
    if not detail:
        return row
    row["url"] = detail_url

    quantity = detail.get("quantity")
    try:
        quantity = int(float(quantity))
    except (TypeError, ValueError):
        quantity = None

    if quantity is not None:
        row["stock_quantity"] = quantity
        row["stock"] = "Stokta" if quantity > 0 else "Tükendi"
    supplier_code = str(
        detail.get("supplier_code") or detail.get("code") or ""
    ).strip()
    if supplier_code:
        row["product_code"] = supplier_code
    return row


@register_scraper("starakim")
class StarakimScraper(BasePriceScraper):
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
            for future in as_completed(future_map):
                category, url = future_map[future]
                try:
                    all_rows.extend(
                        _parse_products(future.result(), category, scraped_at)
                    )
                except Exception as exc:
                    failures.append(f"{url}: {exc}")

        if failures:
            raise RuntimeError(
                f"Star Akım tam katalog çekimi eksik kaldı "
                f"({len(failures)} sayfa): {failures[0]}"
            )

        # Kampanya/alt kategori tekrarlarını URL üzerinden tekilleştir.
        unique = {}
        for row in all_rows:
            key = row.get("url") or (
                row.get("brand"), row.get("product_code"), row.get("product_name")
            )
            unique[key] = row

        rows = list(unique.values())
        if self.brands:
            selected = {brand.casefold() for brand in self.brands}
            rows = [
                row for row in rows
                if (row.get("brand") or "").casefold() in selected
            ]

        # Liste sayfası sadece sepete eklenebilirliği gösteriyor. Gerçek stok adedi,
        # ürün detay kaynağındaki PRODUCT_DATA.quantity alanında yayınlanıyor.
        detail_failures = []
        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
            future_map = {
                pool.submit(_enrich_product_detail, row): row
                for row in rows
                if row.get("url")
            }
            for future in as_completed(future_map):
                try:
                    future.result()
                except Exception as exc:
                    detail_failures.append(
                        f"{future_map[future].get('url')}: {exc}"
                    )

        # Tekil geçici hatalar tüm taramayı düşürmesin; ancak anlamlı veri kaybını
        # sessizce kabul etme.
        if rows and len(detail_failures) / len(rows) > 0.01:
            raise RuntimeError(
                f"Star Akım stok detayı eksik kaldı "
                f"({len(detail_failures)} ürün): {detail_failures[0]}"
            )
        rows.sort(key=lambda row: (
            (row.get("brand") or "").casefold(),
            (row.get("product_name") or "").casefold(),
        ))
        return rows
