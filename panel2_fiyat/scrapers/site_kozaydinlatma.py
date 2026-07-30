"""Koz Aydınlatma tam katalog fiyat ve stok scraper'ı."""
import asyncio
import html as html_lib
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from panel2_fiyat.scrapers import BasePriceScraper, register_scraper
from panel2_fiyat.scrapers.site_aydinlatmadunyam import (
    EXCEL_COLUMNS,
    _URL_PATTERN,
    _append_cache,
    _fetch_text,
    _load_cache,
    _parse_product,
)


BASE_URL = "https://www.kozaydinlatma.com"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
DEFAULT_WORKERS = 8

_PRODUCT_SITEMAP_PATTERN = re.compile(
    r"<loc>([^<]*sitemap_product_[^<]+)</loc>",
    re.I,
)


def _product_sitemap_urls() -> list[str]:
    index = _fetch_text(SITEMAP_URL)
    if not index:
        raise RuntimeError("Koz Aydınlatma site haritası alınamadı.")
    return [
        html_lib.unescape(url)
        for url in _PRODUCT_SITEMAP_PATTERN.findall(index)
    ]


def _sitemap_product_urls(url: str) -> list[str]:
    """Geçici boş sitemap yanıtlarının ürün kaybettirmesini önle."""
    last_error = "boş yanıt"
    for attempt in range(5):
        try:
            text = _fetch_text(url) or ""
            product_urls = [
                html_lib.unescape(value)
                for value in _URL_PATTERN.findall(text)
            ]
            if product_urls:
                return product_urls
            last_error = f"ürün bağlantısı bulunamadı (yanıt: {len(text)} bayt)"
        except Exception as exc:
            last_error = str(exc)
        if attempt < 4:
            time.sleep(1.0 + attempt)
    raise RuntimeError(f"Koz Aydınlatma ürün site haritası alınamadı: {url}: {last_error}")


def _product_urls() -> list[str]:
    sitemap_urls = _product_sitemap_urls()
    product_urls = []
    failures = []
    workers = min(4, max(1, len(sitemap_urls)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(_sitemap_product_urls, url): url
            for url in sitemap_urls
        }
        for future in as_completed(future_map):
            url = future_map.pop(future)
            try:
                product_urls.extend(future.result())
            except Exception as exc:
                failures.append(f"{url}: {exc}")
    if failures:
        raise RuntimeError(
            f"Koz Aydınlatma ürün site haritası eksik kaldı "
            f"({len(failures)} dosya): {failures[0]}"
        )
    return sorted(set(product_urls))


def _fetch_product(url: str, scraped_at: str) -> dict | None:
    page_html = _fetch_text(url)
    if not page_html:
        return None
    row = _parse_product(page_html, url, scraped_at)
    if row is None:
        raise RuntimeError("ürün detay modeli çözülemedi")
    return row


@register_scraper("kozaydinlatma")
class KozAydinlatmaScraper(BasePriceScraper):
    EXPORT_EXCEL = True
    EXCEL_COLUMNS = EXCEL_COLUMNS

    async def scrape(self) -> list[dict]:
        return await asyncio.to_thread(self._scrape_sync)

    def _scrape_sync(self) -> list[dict]:
        scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        urls = _product_urls()

        cache_path = os.getenv("KOZAYDINLATMA_CACHE", "").strip()
        cache = _load_cache(cache_path)
        rows = []
        pending = []
        for url in urls:
            if url in cache:
                cached = cache[url]
                if cached:
                    cached = dict(cached)
                    cached["scraped_at"] = scraped_at
                    rows.append(cached)
            else:
                pending.append(url)

        workers = max(
            1,
            int(
                os.getenv("KOZAYDINLATMA_WORKERS", str(DEFAULT_WORKERS))
                or DEFAULT_WORKERS
            ),
        )
        failures = []
        completed = len(urls) - len(pending)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {
                pool.submit(_fetch_product, url, scraped_at): url
                for url in pending
            }
            for future in as_completed(future_map):
                url = future_map.pop(future)
                try:
                    row = future.result()
                    if row:
                        rows.append(row)
                    _append_cache(cache_path, url, row)
                except Exception as exc:
                    failures.append(f"{url}: {exc}")
                completed += 1
                if completed % 500 == 0 or completed == len(urls):
                    print(
                        f"Koz Aydınlatma: {completed}/{len(urls)} ürün işlendi.",
                        flush=True,
                    )

        if failures:
            raise RuntimeError(
                f"Koz Aydınlatma ürün çekimi eksik kaldı "
                f"({len(failures)} ürün): {failures[0]}"
            )

        rows = list({row["url"]: row for row in rows}.values())
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
