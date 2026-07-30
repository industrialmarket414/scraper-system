"""
Generic Playwright-tabanlı fiyat scraper engine.

Strateji:
  1. Her kategori URL'sine git, scroll ile lazy-load tetikle
  2. ÖNCE JSON-LD Product schema'larını parse et (Schema.org)
  3. Fallback: yaygın ürün kartı selektörlerini dene
  4. Sayfalama site config'inden gelir (URL pattern veya next button)
  5. Bestseller URL'sinden gelen ürünler is_bestseller=True işaretlenir

Her site wrapper'ı sadece config sağlar:
    BASE_URL          : "https://www.x.com"
    CATEGORY_PATHS    : ["/kategori-a", "/kategori-b", ...]
    BESTSELLER_PATH   : "/cok-satanlar"   (opsiyonel)
    PAGINATION        : "?page={n}" | "?pg={n}" | "?tp={n}" | "next-button"
    MAX_PAGES         : 10
"""
import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright, Page, BrowserContext


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


async def _launch_chromium(playwright, *, headless: bool):
    """Playwright Chromium yoksa makinedeki Chrome/Edge ile devam et."""
    launch_args = {
        "headless": headless,
        "args": [
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-extensions",
        ],
    }
    try:
        return await playwright.chromium.launch(**launch_args)
    except Exception as original_error:
        candidates = [
            shutil.which("chromium"),
            shutil.which("chromium-browser"),
            shutil.which("google-chrome"),
            shutil.which("chrome"),
            shutil.which("msedge"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        for executable in candidates:
            if executable and Path(executable).is_file():
                return await playwright.chromium.launch(
                    executable_path=str(executable), **launch_args
                )
        raise original_error


def _parse_tr_price(text: str) -> Optional[float]:
    """'1.234,56 TL' → 1234.56"""
    if not text:
        return None
    s = re.sub(r"[^\d,.]", "", str(text).strip())
    if not s:
        return None
    if "," in s:
        int_part = s.split(",")[0].replace(".", "")
        dec_part = s.split(",")[1]
        s = f"{int_part}.{dec_part}"
    else:
        s = s.replace(".", "")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _parse_percent(text: str) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", str(text))
    return float(m.group(1).replace(",", ".")) if m else None


# ── JSON-LD ürün şeması parser ────────────────────────────────────────────────
async def _extract_jsonld_products(page: Page, base_url: str) -> list[dict]:
    """
    Sayfadaki <script type="application/ld+json"> bloklarından Product şemalarını çıkar.
    Schema.org Product / ItemList yapısını destekler.
    """
    out: list[dict] = []
    try:
        blocks = await page.locator(
            "script[type='application/ld+json']"
        ).all_inner_texts()
    except Exception:
        return []

    for block in blocks:
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        for obj in _walk_jsonld(data):
            t = obj.get("@type")
            types = t if isinstance(t, list) else [t] if t else []
            if not any(tt and "Product" in str(tt) for tt in types):
                continue
            item = _jsonld_to_product(obj, base_url)
            if item:
                out.append(item)
    return out


def _walk_jsonld(node):
    """Tüm nested objeleri (Product/ItemListElement içinde) gez."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_jsonld(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_jsonld(v)


def _jsonld_to_product(p: dict, base_url: str) -> Optional[dict]:
    name  = p.get("name") or ""
    brand = p.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name", "")
    brand = (brand or "").strip()
    sku   = p.get("sku") or p.get("mpn") or p.get("productID") or ""
    url   = p.get("url") or p.get("@id") or ""
    if url and not url.startswith("http"):
        url = urljoin(base_url, url)

    # Offer içinden fiyat
    offers = p.get("offers")
    price = None
    currency = "TRY"
    if isinstance(offers, dict):
        price = offers.get("price") or offers.get("lowPrice")
        currency = offers.get("priceCurrency", "TRY")
    elif isinstance(offers, list) and offers:
        price = offers[0].get("price")
        currency = offers[0].get("priceCurrency", "TRY")

    try:
        price_f = float(price) if price is not None else None
    except (ValueError, TypeError):
        price_f = _parse_tr_price(str(price)) if price is not None else None

    if not name and not sku:
        return None

    return {
        "product_code": str(sku).strip() if sku else "",
        "product_name": name.strip(),
        "brand":        brand,
        "price":        price_f,
        "currency":     currency,
        "url":          url,
    }


# ── CSS fallback parser ───────────────────────────────────────────────────────
_FALLBACK_CARD_SELECTORS = [
    "div.product-item", "article.product-item",
    "div.product-card", "li.product",
    "div[class*='product-list']", "div[class*='ProductItem']",
    "div[class*='product-card']", "div[class*='product_item']",
    "div.productItem", "div.showcase",
    ".product",
]

_FALLBACK_PRICE_SELECTORS = [
    ".showcase-price-new", ".current-price", ".productItem__price",
    ".cart-price", ".cart-soft-price",
    ".price", ".product-price", "[class*='price']:not([class*='old'])",
    "span.new-price",
]
_FALLBACK_OLDPRICE_SELECTORS = [
    ".showcase-price-old", ".product-price-not-discounted",
    ".productItem__priceOff", ".productItem__priceRegular",
    ".cart-text-price",
    ".old-price", ".product-old-price", "[class*='old-price']",
    "[class*='discount-price']", "del", "s.price", "span.line-through",
]
_FALLBACK_NAME_SELECTORS = [
    ".showcase-title", ".product-title", ".productItem__title",
    ".vitrin-product-title",
    ".product-name", "h2 a", "h3 a", "a.product-link",
    "[class*='product-name']", "[class*='product-title']",
]
_FALLBACK_BRAND_SELECTORS = [
    ".showcase-brand", ".brand-title", ".vitrin-product-brand",
    ".product-brand", ".brand", "[class*='brand']",
]


async def _extract_fallback_products(
    page: Page,
    base_url: str,
    site_selectors: Optional[dict] = None,
) -> list[dict]:
    """JSON-LD yoksa class fallback ile çıkar. Site-specific override desteği var."""
    out: list[dict] = []

    # Site-specific card selector varsa onu öncelikli dene
    card_sels = []
    if site_selectors and site_selectors.get("card"):
        card_sels.append(site_selectors["card"])
    card_sels.extend(_FALLBACK_CARD_SELECTORS)

    cards = []
    for sel in card_sels:
        try:
            els = await page.locator(sel).all()
        except Exception:
            els = []
        if len(els) >= 2:  # 2+ kart varsa muhtemelen doğru selektör
            cards = els
            break

    for card in cards:
        try:
            item = await _read_card(card, base_url, site_selectors)
            if item:
                out.append(item)
        except Exception:
            continue
    return out


async def _read_card(card, base_url: str, site_selectors: Optional[dict] = None) -> Optional[dict]:
    sel_name  = ([site_selectors["name"]]  if site_selectors and site_selectors.get("name")  else []) + _FALLBACK_NAME_SELECTORS
    sel_brand = ([site_selectors["brand"]] if site_selectors and site_selectors.get("brand") else []) + _FALLBACK_BRAND_SELECTORS
    sel_price = ([site_selectors["price"]] if site_selectors and site_selectors.get("price") else []) + _FALLBACK_PRICE_SELECTORS
    sel_old   = ([site_selectors["old_price"]] if site_selectors and site_selectors.get("old_price") else []) + _FALLBACK_OLDPRICE_SELECTORS

    name = await _first_text(card, sel_name)
    brand = await _first_text(card, sel_brand)
    price_text = await _first_text(card, sel_price)
    old_price_text = await _first_text(card, sel_old)
    code = await _read_product_code(card, site_selectors)

    # URL
    url = ""
    try:
        link = card.locator("a[href]").first
        if await link.count():
            href = await link.get_attribute("href")
            if href:
                url = urljoin(base_url, href)
    except Exception:
        pass

    if not name and not price_text:
        return None

    if not code:
        code = _infer_product_code(name)

    price = _parse_tr_price(price_text)
    old_price = _parse_tr_price(old_price_text) if old_price_text else None
    discount = None
    if price and old_price and old_price > price:
        discount = round((old_price - price) / old_price * 100, 1)

    return {
        "product_code": code,
        "product_name": name,
        "brand":        brand,
        "price":        price,
        "old_price":    old_price,
        "list_price":   old_price,
        "discount_percent": discount,
        "currency":     "TRY",
        "url":          url,
    }


async def _first_text(card, selectors: list[str]) -> str:
    for sel in selectors:
        try:
            el = card.locator(sel).first
            if await el.count():
                t = (await el.inner_text()).strip()
                if t:
                    return t
        except Exception:
            continue
    return ""


async def _read_product_code(card, site_selectors: Optional[dict]) -> str:
    """Karttan stok/üretici kodunu mümkün olan en güvenli kaynaklardan al."""
    if site_selectors and site_selectors.get("code"):
        selector = site_selectors["code"]
        attr = site_selectors.get("code_attr")
        try:
            el = card.locator(selector).first
            if await el.count():
                value = await el.get_attribute(attr) if attr else await el.inner_text()
                if value:
                    return str(value).strip()
        except Exception:
            pass

    for attr in ("data-product-code", "data-product-sku", "data-sku"):
        try:
            value = await card.get_attribute(attr)
            if value:
                return str(value).strip()
        except Exception:
            continue
    return ""


def _infer_product_code(name: str) -> str:
    """
    Liste kartında ayrı stok kodu yoksa ürün adındaki güçlü model kodunu bulur.
    Ölçü/güç değerleri (8W, 220V, 6500K gibi) ürün kodu sayılmaz.
    """
    if not name:
        return ""
    separated = re.findall(
        r"(?<![\w])[A-Za-z0-9]+(?:[-/.][A-Za-z0-9]+)+(?![\w])", name
    )
    compact = re.findall(r"(?<![\w])[A-Za-z0-9]{4,24}(?![\w])", name)
    candidates = separated + compact
    ignored = re.compile(r"^\d+(?:W|V|A|K|MM|CM|MT|HZ|MAH|LM)$", re.I)
    for candidate in candidates:
        code = candidate.strip("-/.")
        digit_count = sum(ch.isdigit() for ch in code)
        has_letter = any(ch.isalpha() for ch in code)
        if code and has_letter and digit_count >= 2 and not ignored.match(code):
            return code
    return ""


# ── Lazy-load için scroll ─────────────────────────────────────────────────────
async def _scroll_to_bottom(page: Page, max_steps: int = 8):
    last = 0
    for _ in range(max_steps):
        try:
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
        except Exception:
            return
        await page.wait_for_timeout(400)
        try:
            h = await page.evaluate("document.body.scrollHeight")
        except Exception:
            return
        if h == last:
            break
        last = h


# ── Tek kategori sayfasını çek ────────────────────────────────────────────────
async def _scrape_category_page(
    page: Page,
    url: str,
    base_url: str,
    site_selectors: Optional[dict] = None,
    prefer_jsonld: bool = True,
) -> list[dict]:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception:
        return []
    await page.wait_for_timeout(800)
    await _scroll_to_bottom(page)

    # JSON-LD önce (eğer prefer_jsonld=True)
    if prefer_jsonld:
        items = await _extract_jsonld_products(page, base_url)
        if items:
            return items

    # Class-based fallback (veya site_selectors dict varsa onunla)
    return await _extract_fallback_products(page, base_url, site_selectors)


# ── Ana giriş noktası ─────────────────────────────────────────────────────────
async def run_generic_scraper(
    base_url: str,
    category_paths: list[str],
    bestseller_path: Optional[str] = None,
    pagination: str = "?page={n}",
    max_pages: int = 5,
    headless: bool = False,
    site_selectors: Optional[dict] = None,
    prefer_jsonld: bool = True,
) -> list[dict]:
    """
    Tüm kategori URL'lerini ziyaret eder, ürünleri çıkarır.
    Bestseller URL'sinden gelenler is_bestseller=True işaretlenir.
    """
    products: list[dict] = []
    seen_keys: set[str] = set()

    all_paths: list[tuple[str, bool]] = [(p, False) for p in category_paths]
    if bestseller_path:
        all_paths.append((bestseller_path, True))

    async with async_playwright() as p:
        # --disable-dev-shm-usage: uzun taramalarda "Target crashed" önler
        browser = await _launch_chromium(p, headless=headless)
        context = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent=UA,
            ignore_https_errors=True,
        )

        try:
            page = await context.new_page()

            for path, is_bs in all_paths:
                for page_no in range(1, max_pages + 1):
                    url = _build_page_url(base_url, path, pagination, page_no)
                    items = await _scrape_category_page(
                        page, url, base_url,
                        site_selectors=site_selectors,
                        prefer_jsonld=prefer_jsonld,
                    )
                    if not items:
                        if page_no > 1:
                            break  # sayfa tükenmiş
                        else:
                            continue  # 1. sayfa bile yoksa pas

                    new_count = 0
                    for it in items:
                        key = (it.get("url")
                               or it.get("product_code")
                               or it.get("product_name", ""))
                        if not key or key in seen_keys:
                            continue
                        seen_keys.add(key)
                        if is_bs:
                            it["is_bestseller"]    = True
                            it["bestseller_label"] = "Çok Satan"
                        products.append(it)
                        new_count += 1

                    if new_count == 0:
                        break  # yeni ürün gelmiyor, sonraki sayfa anlamsız

        finally:
            await browser.close()

    return products


def _build_page_url(base: str, path: str, pagination: str, n: int) -> str:
    full = urljoin(base, path)
    if n == 1:
        return full
    if "{n}" in pagination:
        sep = "&" if "?" in full else ""
        # pagination is like "?page={n}" — strip leading ? if URL already has ?
        pp = pagination.format(n=n)
        if pp.startswith("?") and "?" in full:
            pp = "&" + pp[1:]
        return full + pp
    return full  # fallback — pagination unsupported
