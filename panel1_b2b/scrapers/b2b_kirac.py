"""
Kıraç Elektrik B2B Scraper
URL    : https://b2b.kiracelektrik.com
Giriş  : E-posta + Şifre (captcha yok)
Çekim  : 31 marka, her marka ?ps=999 ile tek sayfada
Sütunlar: Ürün Kodu | Fiyat | İskonto | Stok | Marka
"""
import asyncio
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import async_playwright, Page
from panel1_b2b.scrapers import BaseB2BScraper, register_scraper

os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(Path.home() / ".cache" / "ms-playwright"),
)

LOGIN_URL = "https://b2b.kiracelektrik.com/bayi-girisi-sayfasi"
PAGE_TIMEOUT_MS = 90_000

ALL_BRANDS = [
    "schneider-electric", "siemens", "legrand", "bticino", "oznur",
    "philips", "osram", "entes", "emas", "phoenix-contact", "weidmuller",
    "bemis", "tp-electric", "pyrsmian", "telemecanique-sensors",
    "kraus-naimer", "hellermanntyton", "iboco", "kardes-elektik",
    "sapiselco", "safak-elektrik", "byes", "birtas", "3m",
    "ortac", "mavigard", "pelsan", "trimbox", "isiso", "mesan", "bosch",
]

# C# XPath karşılıkları
_XPATH_CENTERS  = "./div[contains(@class,'text-center')]"
_XPATH_CLR_SPAN = ".//span[contains(@class,'text-danger') or contains(@class,'text-success')]"


@register_scraper("kirac")
class KiracScraper(BaseB2BScraper):
    COLUMNS = [
        ("product_code",  "Ürün Kodu", 20),
        ("price_text",    "Fiyat",     14),
        ("discount_text", "İskonto",   12),
        ("stock",         "Stok",      15),
        ("min_package_qty", "Min. Paket Adedi", 18),
        ("brand",         "Marka",     20),
    ]

    async def scrape(self) -> list[dict]:
        products = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(viewport={"width": 1280, "height": 900})

            try:
                page = await context.new_page()
                await _do_login(page, self.username, self.password)

                brands = self.brands if self.brands else ALL_BRANDS
                for brand in brands:
                    items = await _scrape_brand(page, brand)
                    products.extend(items)

            finally:
                await browser.close()

        return products


# ── Giriş ─────────────────────────────────────────────────────────────────────
async def _do_login(page: Page, username: str, password: str):
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    await page.wait_for_timeout(3000)

    for sel in [
        "button#cookieAccept",
        "xpath=/html/body/div[1]/div[1]/div[2]/button[1]",
        "xpath=/html/body/div[2]/div[1]/div[2]/button[2]",
    ]:
        try:
            el = page.locator(sel).first
            if await el.count() and await el.is_visible():
                await el.click()
                await page.wait_for_timeout(1000)
        except Exception:
            pass

    await page.locator("#vendor-email").fill(username)
    await page.locator("#vendor-password").fill(password)

    for sel in ["button#cookieAccept", "xpath=/html/body/div[2]/div[1]/div[2]/button[2]"]:
        try:
            el = page.locator(sel).first
            if await el.count() and await el.is_visible():
                await el.click()
        except Exception:
            pass

    await page.locator("#vendor-login-btn").click()
    await page.wait_for_timeout(3000)


# ── Marka verisi çek ──────────────────────────────────────────────────────────
async def _scrape_brand(page: Page, brand: str) -> list[dict]:
    await page.goto(
        f"https://b2b.kiracelektrik.com/{brand}?ps=999",
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT_MS,
    )
    await page.wait_for_timeout(3000)

    # Lazy-load içerik için sayfayı aşağı kaydır
    last_height = await page.evaluate("document.body.scrollHeight")
    while True:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2000)
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(1000)

    sel_primary  = "div.product-list-grid.product-row.product-item.product-item-list"
    sel_fallback = "div.product-item.product-item-list"

    try:
        await page.wait_for_selector(sel_primary, timeout=10_000)
    except Exception:
        pass

    product_els = await page.locator(sel_primary).all()
    if not product_els:
        product_els = await page.locator(sel_fallback).all()

    items = []
    for el in product_els:
        item = await _extract_product(el, brand)
        if item:
            items.append(item)

    semaphore = asyncio.Semaphore(10)

    async def _fill_product_details(item: dict):
        async with semaphore:
            details = await _get_product_details(page, item.pop("_product_url", ""))
            item["min_package_qty"] = details["min_package_qty"]

            # Bazı ürünler listede "Fiyat Sorunuz" olarak gösterilse de detay
            # sayfasındaki gömülü product verisinde liste fiyatı ve iskonto bulunur.
            if item["price_text"] == "#" and details["list_price_text"]:
                item["price_text"] = details["list_price_text"]
            if (
                _is_zero_or_missing_discount(item["discount_text"])
                and details["discount_text"]
            ):
                item["discount_text"] = details["discount_text"]
            item["discount_percent"] = _parse_discount_percent(
                item["discount_text"]
            )

    await asyncio.gather(*(_fill_product_details(item) for item in items))

    return items


def _json_field(html: str, field: str) -> str:
    """Sayfadaki gömülü product JSON'undan tek bir scalar alanı döndür."""
    match = re.search(
        rf'["\']{re.escape(field)}["\']\s*:\s*(?:"([^"]*)"|\'([^\']*)\'|(-?[0-9]+(?:[.,][0-9]+)?))',
        html,
    )
    if not match:
        return ""
    return next((value for value in match.groups() if value is not None), "")


def _format_decimal_tr(raw: str) -> str:
    """JSON sayısını Kıraç listesindeki 1.260,00 biçimine çevir."""
    try:
        value = Decimal(raw.replace(",", "."))
    except (InvalidOperation, AttributeError):
        return ""
    formatted = f"{value:,.2f}"
    return formatted.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _format_discount(raw: str) -> str:
    try:
        value = Decimal(raw.replace(",", "."))
    except (InvalidOperation, AttributeError):
        return ""
    text = format(value.normalize(), "f").replace(".", ",")
    return f"% {text}"


def _parse_discount_percent(raw: str) -> float | None:
    """`% 71`, `71,5` ve `0` biçimlerini sayısal yüzdeye çevir."""
    if not raw:
        return None
    cleaned = raw.replace("%", "").replace(" ", "").replace(",", ".")
    if not cleaned or cleaned == "#":
        return None
    try:
        return float(Decimal(cleaned))
    except InvalidOperation:
        return None


def _is_zero_or_missing_discount(raw: str) -> bool:
    """Liste değeri boş, # veya 0 ise detay JSON'undaki iskontoya izin ver."""
    parsed = _parse_discount_percent(raw)
    return parsed is None or parsed == 0


async def _get_product_details(page: Page, product_url: str) -> dict[str, str]:
    details = {
        "min_package_qty": "#",
        "list_price_text": "",
        "discount_text": "",
    }
    if not product_url:
        return details

    try:
        response = await page.context.request.get(product_url, timeout=PAGE_TIMEOUT_MS)
        if not response.ok:
            return details

        html = await response.text()
        match = re.search(
            r'["\']?MIN_ORDER_COUNT["\']?\s*:\s*["\']?([0-9]+(?:[.,][0-9]+)?)',
            html,
        )
        if match:
            details["min_package_qty"] = match.group(1).replace(",", ".")

        list_price = _json_field(html, "PRICE_NOT_DISCOUNTED")
        currency = (
            _json_field(html, "TARGET_CURRENCY")
            or _json_field(html, "CURRENCY")
        )
        discount = (
            _json_field(html, "DISCOUNT_PERCENT_RAW")
            or _json_field(html, "DISCOUNT_PERCENT")
        )

        formatted_price = _format_decimal_tr(list_price)
        if formatted_price:
            details["list_price_text"] = (
                f"{formatted_price} {currency}".strip()
            )
        details["discount_text"] = _format_discount(discount)
        return details
    except Exception:
        return details


# ── Tek ürün verisi — C# XPath mantığının birebir karşılığı ──────────────────
async def _extract_product(el, brand: str) -> dict | None:
    product_code = "#"
    price_text   = "#"
    iskonto_text = "#"
    stock        = "#"
    brand_name   = brand
    product_url  = ""

    # Marka
    try:
        b = el.locator("div.product-info-cell > div > div.fw-bold.brand-title a").first
        if await b.count():
            brand_name = (await b.inner_text()).strip() or brand
    except Exception:
        pass

    # Ürün kodu (başlığın ilk kelimesi)
    try:
        t = el.locator("div.product-info-cell > div > div:nth-of-type(2) > a").first
        if await t.count():
            full = (await t.inner_text()).strip()
            product_code = full.split()[0] if full else "#"
            href = await t.get_attribute("href")
            if href:
                product_url = urljoin("https://b2b.kiracelektrik.com", href)
    except Exception:
        pass

    # ─────────────────────────────────────────────────────────────────────────
    # ASIL DÜZELTME:
    # C# kodu "./div[contains(@class,'text-center')]" kullanıyor —
    # bu SADECE doğrudan çocuk div'leri alır (iç içe olanları değil).
    # Playwright'ta XPath ile birebir eşleştirildi.
    # ─────────────────────────────────────────────────────────────────────────
    try:
        centers = el.locator(f"xpath={_XPATH_CENTERS}")
        cnt = await centers.count()

        if cnt >= 3:
            # centers[0] = Fiyat, centers[1] = İskonto, centers[2] = Stok
            price_text   = (await centers.nth(0).inner_text()).strip() or "#"
            iskonto_text = (await centers.nth(1).inner_text()).strip() or "#"
            stock        = (await centers.nth(2).inner_text()).strip() or "#"

            # Boşsa renk spanına bak (C# ile aynı fallback)
            async def _span_text(c):
                sp = c.locator(f"xpath={_XPATH_CLR_SPAN}").first
                return (await sp.inner_text()).strip() if await sp.count() else ""

            if price_text == "#":
                price_text = await _span_text(centers.nth(0)) or "#"
            if iskonto_text == "#":
                iskonto_text = await _span_text(centers.nth(1)) or "#"
            if stock == "#":
                stock = await _span_text(centers.nth(2)) or "#"

        else:
            # Fallback: ilk text-danger span
            sp = el.locator("div.text-center span.text-danger").first
            if await sp.count():
                stock = (await sp.inner_text()).strip()

    except Exception:
        try:
            sp = el.locator(f"xpath={_XPATH_CLR_SPAN}").first
            if await sp.count():
                stock = (await sp.inner_text()).strip()
        except Exception:
            pass

    if product_code == "#" and brand_name == brand:
        return None

    return {
        "product_code":  product_code,
        "price_text":    price_text,
        "discount_text": iskonto_text,
        "stock":         stock,
        "min_package_qty": "#",
        "brand":         brand_name,
        "_product_url":  product_url,
    }
