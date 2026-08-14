"""Aydınlatma Dünyam tam katalog fiyat ve stok scraper'ı."""
import asyncio
import html as html_lib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests

from panel2_fiyat.cancel import ScrapeCancelled
from panel2_fiyat.scrapers import BasePriceScraper, register_scraper


BASE_URL = "https://www.aydinlatmadunyam.com"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
REQUEST_TIMEOUT = (10, 25)
DEFAULT_WORKERS = 8

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

_PRODUCT_SITEMAP_PATTERN = re.compile(
    r"<loc>([^<]*sitemap_product_[^<]+)</loc>",
    re.I,
)
_URL_PATTERN = re.compile(r"<loc>(https?://[^<]+)</loc>", re.I)
_PRODUCT_DATA_PATTERN = re.compile(
    r"\bpageParams\s*=\s*\{\s*product\s*:\s*\{([\s\S]*?)\}\s*\}\s*;",
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


def _raise_if_stopped(should_stop=None) -> None:
    if should_stop and should_stop():
        raise ScrapeCancelled("Aydinlatma Dunyam cekimi durduruldu")


def _sleep_with_cancel(seconds: float, should_stop=None) -> None:
    end_at = time.time() + max(0, seconds)
    while time.time() < end_at:
        _raise_if_stopped(should_stop)
        time.sleep(min(0.25, end_at - time.time()))


def _fetch_text(url: str, should_stop=None) -> str | None:
    last_error = None
    for attempt in range(6):
        _raise_if_stopped(should_stop)
        try:
            response = _session().get(url, timeout=REQUEST_TIMEOUT)
            _raise_if_stopped(should_stop)
            if response.status_code in {404, 410}:
                return None
            if response.status_code == 429:
                wait = float(response.headers.get("Retry-After") or 3 + attempt * 2)
                _sleep_with_cancel(min(wait, 20), should_stop)
                continue
            response.raise_for_status()
            response.encoding = "utf-8"
            return response.text
        except Exception as exc:
            if isinstance(exc, ScrapeCancelled):
                raise
            last_error = exc
            if attempt < 5:
                _sleep_with_cancel(1.0 + attempt * 1.5, should_stop)
    raise RuntimeError(f"Aydınlatma Dünyam sayfası alınamadı: {url}: {last_error}")


def _number(value):
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else round(number, 4)


def _money(value):
    number = _number(value)
    return round(number, 2) if number is not None else None


def _js_string(block: str, key: str) -> str:
    match = re.search(
        rf"\b{re.escape(key)}\s*:\s*\"((?:\\.|[^\"\\])*)\"",
        block,
    )
    if not match:
        return ""
    try:
        return json.loads(f'"{match.group(1)}"')
    except (TypeError, ValueError, json.JSONDecodeError):
        return match.group(1)


def _js_number(block: str, key: str):
    match = re.search(
        rf"\b{re.escape(key)}\s*:\s*(-?\d+(?:\.\d+)?)",
        block,
    )
    return _number(match.group(1)) if match else None


def _parse_product(html: str, url: str, scraped_at: str) -> dict | None:
    match = _PRODUCT_DATA_PATTERN.search(html)
    if not match:
        return None
    block = match.group(1)

    sale_price = _money(_js_number(block, "salePrice"))
    tax = _number(_js_number(block, "tax")) or 0
    list_excluding_tax = _number(_js_number(block, "priceWithCurrency"))
    source_list_price = (
        _money(list_excluding_tax * (1 + tax / 100))
        if list_excluding_tax is not None
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

    stock_match = _STOCK_PATTERN.search(html)
    stock_quantity = _number(stock_match.group(1)) if stock_match else 0
    return {
        "brand": _js_string(block, "brandName").strip() or "Markasız",
        "product_code": _js_string(block, "sku").strip(),
        "product_name": _js_string(block, "fullName").strip(),
        "stock": "Stokta" if stock_quantity is not None and stock_quantity > 0 else "Tükendi",
        "stock_quantity": stock_quantity,
        "discount_percent": discount,
        "list_price": list_price,
        "old_price": list_price if list_price != sale_price else None,
        "price": sale_price,
        "currency": "TRY",
        "category": _js_string(block, "categoryName").strip(),
        "url": url,
        "scraped_at": scraped_at,
    }


def _product_sitemap_urls(should_stop=None) -> list[str]:
    index = _fetch_text(SITEMAP_URL, should_stop)
    if not index:
        raise RuntimeError("Aydınlatma Dünyam site haritası alınamadı.")
    return [
        html_lib.unescape(url)
        for url in _PRODUCT_SITEMAP_PATTERN.findall(index)
    ]


def _product_urls(should_stop=None) -> list[str]:
    sitemap_urls = _product_sitemap_urls(should_stop)
    product_urls = []
    failures = []
    workers = min(4, max(1, len(sitemap_urls)))
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        future_map = {
            pool.submit(_fetch_text, url, should_stop): url
            for url in sitemap_urls
        }
        while future_map:
            _raise_if_stopped(should_stop)
            try:
                future = next(as_completed(list(future_map), timeout=1))
            except TimeoutError:
                continue
            url = future_map.pop(future)
            try:
                text = future.result() or ""
                product_urls.extend(
                    html_lib.unescape(value)
                    for value in _URL_PATTERN.findall(text)
                )
            except Exception as exc:
                failures.append(f"{url}: {exc}")
    finally:
        pool.shutdown(
            wait=not (should_stop and should_stop()),
            cancel_futures=bool(should_stop and should_stop()),
        )
    if failures:
        raise RuntimeError(
            f"Aydınlatma Dünyam ürün site haritası eksik kaldı "
            f"({len(failures)} dosya): {failures[0]}"
        )
    return sorted(set(product_urls))


def _load_cache(path: str) -> dict[str, dict | None]:
    cache = {}
    if not path or not os.path.exists(path):
        return cache
    try:
        with open(path, "r", encoding="utf-8") as cache_file:
            for line in cache_file:
                try:
                    item = json.loads(line)
                    url = str(item.get("url") or "")
                    if url:
                        cache[url] = item.get("row")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
    except OSError:
        return {}
    return cache


def _append_cache(path: str, url: str, row: dict | None) -> None:
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as cache_file:
        cache_file.write(json.dumps({
            "url": url,
            "row": row,
        }, ensure_ascii=False) + "\n")


def _fetch_product(url: str, scraped_at: str, should_stop=None) -> dict | None:
    _raise_if_stopped(should_stop)
    html = _fetch_text(url, should_stop)
    _raise_if_stopped(should_stop)
    return _parse_product(html, url, scraped_at) if html else None


@register_scraper("aydinlatmadunyam")
class AydinlatmaDunyamScraper(BasePriceScraper):
    EXPORT_EXCEL = True
    EXCEL_COLUMNS = EXCEL_COLUMNS

    async def scrape(self) -> list[dict]:
        return await asyncio.to_thread(self._scrape_sync)

    def _scrape_sync(self) -> list[dict]:
        scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if self.should_stop():
            raise ScrapeCancelled("Aydinlatma Dunyam cekimi durduruldu")
        urls = _product_urls(self.should_stop)
        if self.should_stop():
            raise ScrapeCancelled("Aydinlatma Dunyam cekimi durduruldu")
        limit = int(os.getenv("AYDINLATMADUNYAM_MAX_PRODUCTS", "0") or 0)
        if limit > 0:
            urls = urls[:limit]
        print(
            f"Aydinlatma Dunyam: {len(urls)} urun icin cekim basladi.",
            flush=True,
        )

        cache_root = os.getenv(
            "SCRAPER_CACHE_DIR",
            os.path.join(os.getcwd(), "cache"),
        ).strip()
        os.makedirs(cache_root, exist_ok=True)

        cache_date = datetime.now().strftime("%Y-%m-%d")
        cache_path = os.path.join(
            cache_root,
            f"aydinlatmadunyam_{cache_date}.jsonl",
        )

        # Günlük cache yalnızca yarım kalan bir çekime özellikle devam edilmek
        # istendiğinde kullanılır. Varsayılan davranış tüm ürünleri siteden
        # yeniden çekmektir; böylece aynı gün yapılan ikinci çalıştırmada eski
        # fiyat ve stoklar güncelmiş gibi rapora taşınmaz.
        resume_cache = os.getenv(
            "AYDINLATMADUNYAM_RESUME_CACHE",
            "0",
        ).strip().casefold() in {"1", "true", "yes", "evet"}

        if resume_cache:
            cache = _load_cache(cache_path)
            if cache:
                print(
                    f"Aydınlatma Dünyam: {len(cache)} ürünlük yarım çekime "
                    "cache üzerinden devam ediliyor.",
                    flush=True,
                )
        else:
            cache = {}
            try:
                os.remove(cache_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RuntimeError(
                    f"Aydınlatma Dünyam eski cache dosyası silinemedi: "
                    f"{cache_path}: {exc}"
                ) from exc
            print(
                "Aydınlatma Dünyam: güncel fiyat ve stok için tüm ürünler "
                "yeniden çekilecek.",
                flush=True,
            )

        rows = []
        pending = []
        for url in urls:
            if self.should_stop():
                raise ScrapeCancelled("Aydinlatma Dunyam cekimi durduruldu")
            if url in cache:
                cached = cache[url]
                if cached:
                    # Cache satırının gerçek çekim tarihini koru; yeni çekilmiş
                    # gibi güncel zaman damgası verme.
                    rows.append(dict(cached))
            else:
                pending.append(url)

        workers = max(
            1,
            int(
                os.getenv(
                    "AYDINLATMADUNYAM_WORKERS",
                    str(DEFAULT_WORKERS),
                )
                or DEFAULT_WORKERS
            ),
        )
        failures = []
        completed = len(urls) - len(pending)
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            pending_urls = iter(pending)
            future_map = {}
            for _ in range(workers):
                try:
                    url = next(pending_urls)
                except StopIteration:
                    break
                future_map[pool.submit(
                    _fetch_product,
                    url,
                    scraped_at,
                    self.should_stop,
                )] = url
            while future_map:
                if self.should_stop():
                    raise ScrapeCancelled("Aydinlatma Dunyam cekimi durduruldu")
                try:
                    future = next(as_completed(list(future_map), timeout=1))
                except TimeoutError:
                    continue
                url = future_map.pop(future)
                try:
                    row = future.result()
                    if row:
                        rows.append(row)
                    _append_cache(cache_path, url, row)
                except Exception as exc:
                    failures.append(f"{url}: {exc}")
                completed += 1
                if not self.should_stop():
                    try:
                        next_url = next(pending_urls)
                    except StopIteration:
                        pass
                    else:
                        future_map[pool.submit(
                            _fetch_product,
                            next_url,
                            scraped_at,
                            self.should_stop,
                        )] = next_url
                if completed % 250 == 0 or completed == len(urls):
                    print(
                        f"Aydınlatma Dünyam: {completed}/{len(urls)} "
                        "ürün işlendi.",
                        flush=True,
                    )

        finally:
            pool.shutdown(
                wait=not self.should_stop(),
                cancel_futures=self.should_stop(),
            )

        if failures:
            raise RuntimeError(
                f"Aydınlatma Dünyam ürün çekimi eksik kaldı "
                f"({len(failures)} ürün): {failures[0]}"
            )

        unique = {row["url"]: row for row in rows}
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
