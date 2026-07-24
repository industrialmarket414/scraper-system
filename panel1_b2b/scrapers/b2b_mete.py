"""
Met Aydınlatma B2B Scraper
URL    : https://www.metaydinlatma.com
Giriş  : Gerekmiyor
Çekim  : /marka/{brand}?tp={page} → ürün linkleri → detay sayfaları
Sütunlar: Stok Kodu | Kalan Stok | İskonto | Fiyat | URL
"""
import os
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlencode, parse_qs, urlunparse

from playwright.async_api import async_playwright, Page
from panel1_b2b.scrapers import BaseB2BScraper, register_scraper

os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(Path.home() / ".cache" / "ms-playwright"),
)

BASE_URL   = "https://www.metaydinlatma.com"
MAX_PAGES  = 22   # C# sabitiyle aynı
SCROLL_STEPS = 6  # lazy-load için

# Ayarlardan marka gelmezse varsayılan (C# kodundaki START_URL markası)
DEFAULT_BRANDS = ["noas"]


@register_scraper("mete")
class MeteScraper(BaseB2BScraper):
    COLUMNS = [
        ("product_code",  "Stok Kodu",   20),
        ("stock",         "Kalan Stok",  15),
        ("min_package_qty", "Min. Paket Adedi", 18),
        ("discount_text", "İskonto",     12),
        ("price_text",    "Fiyat",       16),
        ("url",           "URL",         40),
    ]

    async def scrape(self) -> list[dict]:
        products = []
        brands = self.brands if self.brands else DEFAULT_BRANDS

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(viewport={"width": 1400, "height": 900})

            try:
                page = await context.new_page()

                for brand in brands:
                    links = await _collect_links(page, brand)
                    for url in links:
                        item = await _read_detail(page, url)
                        if item:
                            products.append(item)

            finally:
                await browser.close()

        return products


# ── Liste sayfalarından ürün linklerini topla ─────────────────────────────────
async def _collect_links(page: Page, brand: str) -> list[str]:
    seen: set[str] = set()
    start_url = f"{BASE_URL}/marka/{brand}"

    for page_num in range(1, MAX_PAGES + 1):
        list_url = start_url if page_num == 1 else _set_tp(start_url, page_num)
        await page.goto(list_url, wait_until="domcontentloaded", timeout=30_000)

        # Liste konteynerini bekle
        try:
            await page.wait_for_selector("#product-list-container", timeout=15_000)
        except Exception:
            if page_num > 1:
                break
            continue

        # Lazy-load tetikle (C# ile aynı)
        for _ in range(SCROLL_STEPS):
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            await page.wait_for_timeout(300)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(200)

        cards = await page.locator(
            "#product-list-container .showcase-container .showcase"
        ).all()

        if not cards:
            if page_num > 1:
                break
            continue

        page_links: set[str] = set()
        for card in cards:
            # Önce .showcase-label-container a
            el = card.locator(".showcase-label-container a[href]").first
            if await el.count():
                href = await el.get_attribute("href")
                norm = _normalize(href)
                if norm:
                    page_links.add(norm)
                    continue

            # Fallback: kart içindeki her a[href]
            for a in await card.locator("a[href]").all():
                href = await a.get_attribute("href")
                norm = _normalize(href)
                if norm:
                    page_links.add(norm)
                    break

        if not page_links and page_num > 1:
            break

        seen |= page_links

    return list(seen)


# ── Ürün detay sayfasını oku ──────────────────────────────────────────────────
async def _read_detail(page: Page, url: str) -> dict | None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_selector("div.product-list-container", timeout=15_000)
    except Exception:
        return None

    stok = kalan = min_package_qty = price = discount = ""

    # Stok Kodu + Kalan Stok — sadece doğrudan çocuk div.product-list-row (C# XPath mantığı)
    rows = page.locator("div.product-list-container > div.product-list-row")
    for i in range(await rows.count()):
        row = rows.nth(i)
        try:
            title_el = row.locator(".product-list-title").first
            val_el   = row.locator(".product-list-content").first
            title = (await title_el.inner_text()).strip().lower() if await title_el.count() else ""
            val   = _clean((await val_el.inner_text()).strip() if await val_el.count() else "")

            if title == "stok kodu" and val:
                stok = val
            elif "kalan stok" in title and val:
                kalan = val
        except Exception:
            pass

    # Fiyat: önce old_price, yoksa new_price (C# mantığıyla aynı)
    try:
        old_el = page.locator(".product-price-old").first
        new_el = page.locator(".product-price-new").first
        old_txt = _clean(await old_el.inner_text()) if await old_el.count() else ""
        new_txt = _clean(await new_el.inner_text()) if await new_el.count() else ""
        price = old_txt or new_txt
    except Exception:
        pass

    # İskonto: .discount-label içinde "%45 İndirim" gibi → sadece rakamı al
    try:
        disc_el = page.locator(".discount-label").first
        if await disc_el.count():
            disc_txt = await disc_el.inner_text()
            m = re.search(r'%?\s*(\d{1,3})', disc_txt)
            discount = m.group(1) if m else "0"
        else:
            discount = "0"
    except Exception:
        discount = "0"

    # Minimum sipariş/paket adedi: stoklu ürünlerde miktar kutusunun gerçek
    # min/step/value değeri; stok yoksa sayfanın satış-katı mesajındaki değer.
    try:
        qty_input = page.locator(
            '#qty-input, [data-selector="qty"]'
        ).first
        if await qty_input.count():
            candidates = [
                await qty_input.get_attribute("data-min-quantity"),
                await qty_input.get_attribute("data-package-quantity"),
                await qty_input.get_attribute("min"),
                await qty_input.get_attribute("step"),
                await qty_input.evaluate("el => el.value"),
            ]
            min_package_qty = _first_positive_number(candidates)

        if not min_package_qty:
            html = await page.content()
            match = re.search(
                r'["\']product_quantity_multiple["\']\s*:\s*["\'][^"\']*?'
                r'(\d+(?:[.,]\d+)?)\s+adet\s+ve\s+katlar',
                html,
                flags=re.IGNORECASE,
            )
            if match:
                min_package_qty = _format_number(match.group(1))
    except Exception:
        min_package_qty = ""

    if not stok and not kalan:
        return None

    return {
        "product_code":  stok    or "#",
        "stock":         kalan   or "#",
        "min_package_qty": min_package_qty,
        "discount_text": discount or "0",
        "price_text":    price   or "0",
        "url":           url,
    }


# ── Yardımcılar ───────────────────────────────────────────────────────────────
def _set_tp(url: str, page_num: int) -> str:
    """URL'ye ?tp={page_num} ekle (C#'taki SetTp metoduyla aynı)."""
    p = urlparse(url)
    q = parse_qs(p.query)
    q["tp"] = [str(page_num)]
    new_q = urlencode({k: v[0] for k, v in q.items()})
    return urlunparse(p._replace(query=new_q))


def _normalize(href: str | None) -> str | None:
    if not href or not href.strip():
        return None
    href = href.strip()
    if href.startswith("//"):
        return "https:" + href
    if href.lower().startswith("http"):
        return href
    return urljoin(BASE_URL, href)


def _clean(s: str) -> str:
    if not s:
        return s
    s = s.replace(" ", " ").strip()
    return re.sub(r"\s+", " ", s)


def _first_positive_number(values) -> str:
    for value in values:
        formatted = _format_number(value)
        if formatted:
            return formatted
    return ""


def _format_number(value) -> str:
    if value is None:
        return ""
    match = re.search(r"\d+(?:[.,]\d+)?", str(value))
    if not match:
        return ""
    number = float(match.group().replace(",", "."))
    if number <= 0:
        return ""
    return str(int(number)) if number.is_integer() else str(number)
