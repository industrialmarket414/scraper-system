"""
Yıldız SDE B2B Scraper
URL    : https://b2b.yildizsde.com.tr
Giriş  : E-posta + Şifre + Captcha (OCR)
Çekim  : Tüm 29 marka, her marka için max 30 sayfa
Sütunlar: Marka | Ürün Kodu | Stok | Min. Paket Adedi | İskonto | Fiyat
"""
import io
import json
import os
import re
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page, BrowserContext
from panel1_b2b.scrapers import BaseB2BScraper, register_scraper

os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(Path.home() / ".cache" / "ms-playwright"),
)

# ── Sabitler ──────────────────────────────────────────────────────────────────
LOGIN_URL        = "https://b2b.yildizsde.com.tr/Login/Login/?ReturnUrl=%2F"
LIST_BASE        = "https://b2b.yildizsde.com.tr/Home/ProductList/"
COOKIES_FILE     = Path(__file__).parent.parent.parent / "config" / "cookies_yildiz.json"
MAX_PAGES        = 30
PAGE_WAIT_MS     = 5000   # sayfa geçişleri arası bekleme (ms)
LOGIN_WAIT_MS    = 2500
MAX_CAPTCHA_TRY  = 5
ACK_BRAND_RETRIES = 2
ACK_SEARCH_RETRIES = 3
ACK_RETRY_DELAYS = (3000, 7000, 15000)

# Tüm markalar — marka seçimi yok, hepsi otomatik çekilir
ALL_BRANDS = [
    "ACK", "ARTLİNE", "ATILIM", "AUDIO", "BTİCİNO", "BYLION", "CATA",
    "ÇETİNKAYA", "ERAT", "ERSE", "GLOBALE", "GWEST", "HCS", "HES",
    "IŞILDAR", "JUPİTER", "LEGRAND", "MUTLUSAN", "NADE", "NEAD",
    "NEXANS", "ÖZNUR", "PRYSMİAN", "STANLEY", "TAMER", "THEA",
    "ÜNAL", "VİKO", "WAGO",
]


# ── Yardımcı: fiyat / iskonto parse ──────────────────────────────────────────
def _parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    text = re.sub(r'[^\d,.]', '', text.strip())
    if ',' in text:
        int_part = text.split(',')[0].replace('.', '')
        dec_part = text.split(',')[1]
        text = f"{int_part}.{dec_part}"
    else:
        text = text.replace('.', '')
    try:
        return float(text) if text else None
    except ValueError:
        return None


def _parse_discount(text: str) -> Optional[float]:
    if not text:
        return None
    m = re.search(r'[\d]+(?:[.,]\d+)?', text)
    return float(m.group().replace(',', '.')) if m else None


# ── Cookie yönetimi ───────────────────────────────────────────────────────────
async def _save_cookies(ctx: BrowserContext):
    try:
        COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        COOKIES_FILE.write_text(
            json.dumps(await ctx.cookies(), ensure_ascii=False, indent=2)
        )
    except Exception:
        pass


async def _load_cookies(ctx: BrowserContext) -> bool:
    try:
        if not COOKIES_FILE.exists():
            return False
        data = json.loads(COOKIES_FILE.read_text())
        if not data:
            return False
        await ctx.add_cookies(data)
        return True
    except Exception:
        return False


# ── Captcha OCR ───────────────────────────────────────────────────────────────
async def _read_captcha_ocr(page: Page) -> Optional[str]:
    """
    #img-captcha elemanının ekran görüntüsünü alır,
    pytesseract ile metni okur.
    pytesseract kurulu değilse None döner.
    """
    try:
        import pytesseract
        from PIL import Image

        img_el = page.locator("#img-captcha")
        if not await img_el.count():
            return None

        png = await img_el.screenshot()
        img = Image.open(io.BytesIO(png)).convert("L")
        # OCR doğruluğunu artır: 3× büyüt
        img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)

        text = pytesseract.image_to_string(
            img,
            lang="tur+eng",
            config=(
                "--psm 8 --oem 3 "
                "-c tessedit_char_whitelist="
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
            ),
        )
        return text.strip() or None
    except ImportError:
        return None
    except Exception:
        return None


# ── Giriş ─────────────────────────────────────────────────────────────────────
async def _do_login(page: Page, username: str, password: str) -> bool:
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(1000)

    email_el = page.locator("#email")
    pass_el  = page.locator("#password")

    if not await email_el.count() or not await pass_el.count():
        return False

    await email_el.fill(username)
    # focusout ile firma/bayi bilgisi tetiklensin
    await page.locator("#password").click()
    await page.wait_for_timeout(800)
    await pass_el.fill(password)

    for attempt in range(1, MAX_CAPTCHA_TRY + 1):
        captcha_inp = page.locator("#captcha")

        if await captcha_inp.count():
            captcha_text = await _read_captcha_ocr(page)
            if captcha_text:
                await captcha_inp.fill(captcha_text)

        # Login butonuna tıkla
        try:
            btn = page.locator("a[onclick*='Login()']").first
            if await btn.count():
                await btn.evaluate("el => el.click()")
            else:
                if await captcha_inp.count():
                    await captcha_inp.press("Enter")
                else:
                    await pass_el.press("Enter")
        except Exception:
            if await captcha_inp.count():
                await captcha_inp.press("Enter")

        await page.wait_for_timeout(LOGIN_WAIT_MS)

        if "/login" not in page.url.lower():
            return True  # giriş başarılı

        # Başarısız — captcha yenile
        if attempt < MAX_CAPTCHA_TRY:
            try:
                img_el = page.locator("#img-captcha").first
                if await img_el.count():
                    await img_el.evaluate("el => el.click()")
                await page.wait_for_timeout(1200)
            except Exception:
                pass

    return "/login" not in page.url.lower()


# ── Sonraki sayfa butonu ──────────────────────────────────────────────────────
async def _click_next(page: Page) -> bool:
    """Sonraki sayfa butonunu bul ve tıkla. Yoksa False döndür."""
    css_selectors = [
        "a[rel='next']", "button[rel='next']",
        "#product-table_next a", "a.paginate_button.next:not(.disabled)",
        "li.next:not(.disabled) a", ".pagination a.next",
    ]
    for sel in css_selectors:
        try:
            el = page.locator(sel).first
            if not await el.count():
                continue
            cls = (await el.get_attribute("class")) or ""
            if "disabled" in cls:
                return False
            await el.scroll_into_view_if_needed()
            await el.evaluate("el => el.click()")
            return True
        except Exception:
            continue

    for text in ["Sonraki", "İleri", "Next", "›", "»"]:
        try:
            el = page.locator(f"a:has-text('{text}')").first
            if not await el.count():
                continue
            cls = (await el.get_attribute("class")) or ""
            if "disabled" in cls:
                return False
            await el.evaluate("el => el.click()")
            return True
        except Exception:
            continue

    return False


# ── Scraper sınıfı ────────────────────────────────────────────────────────────
@register_scraper("yildiz")
class YildizScraper(BaseB2BScraper):
    COLUMNS = [
        ("brand",         "Marka",      20),
        ("product_code",  "Ürün Kodu",  20),
        ("stock",         "Stok",       15),
        ("min_package_quantity", "Min. Paket Adedi", 18),
        ("discount_text", "İskonto",    12),
        ("price_text",    "Fiyat",      14),
    ]

    async def scrape(self) -> list[dict]:
        products = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context  = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                ignore_https_errors=True,   # site http/https karışık kullanıyor
            )

            try:
                # Kaydedilmiş cookie ile oturumu sürdür
                await _load_cookies(context)

                page = await context.new_page()
                await page.goto(
                    "https://b2b.yildizsde.com.tr/",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )

                # Cookie geçersizse yeniden giriş yap
                if "/login" in page.url.lower() or not page.url.startswith("https://b2b.yildizsde"):
                    ok = await _do_login(page, self.username, self.password)
                    if not ok:
                        raise Exception(
                            "Yıldız B2B girişi başarısız — "
                            "kullanıcı adı/şifre veya captcha hatası."
                        )
                    await _save_cookies(context)

                # Ayarlardan marka listesi geldiyse onu kullan, yoksa hepsi
                brands = self.brands if self.brands else ALL_BRANDS

                for brand in brands:
                    brand_items = await self._scrape_brand(page, brand)
                    products.extend(brand_items)

            finally:
                await browser.close()

        return products

    async def _load_brand_first_page(self, page: Page, brand: str) -> bool:
        """
        Marka sayfasını açar.

        Yıldız sunucusu ACK sorgusunda zaman zaman kendi SQL timeout'una
        düşüyor. Bu hata tarayıcı timeout'u değildir; aynı doğru URL kontrollü
        biçimde yeniden denenir.
        """
        is_ack = brand.upper() == "ACK"
        routes = [(f"{LIST_BASE}1&{brand}", 1, "marka")]
        if is_ack:
            # TYPE=1 ACK sorgusu Yıldız sunucusunda SQL timeout'a düşebiliyor.
            # TYPE=2 aynı ürünleri genel arama prosedürü üzerinden getiriyor.
            routes.append((f"{LIST_BASE}2&{brand}", 2, "genel arama"))

        for url, route_type, route_name in routes:
            attempts = 1
            if is_ack and route_type == 1:
                attempts = ACK_BRAND_RETRIES
            elif is_ack and route_type == 2:
                attempts = ACK_SEARCH_RETRIES

            for attempt in range(attempts):
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=45_000,
                )
                try:
                    await page.wait_for_selector(
                        "tbody#product-table-body tr",
                        timeout=45_000,
                    )
                    if is_ack and route_type == 2:
                        print(
                            "[Yıldız] ACK, genel arama sorgusu üzerinden yüklendi.",
                            flush=True,
                        )
                    return True
                except Exception:
                    server_timeout = await page.locator(
                        "text=Timeout expired"
                    ).count()
                    if server_timeout:
                        print(
                            f"[Yıldız] {brand} {route_name} sorgusu zaman "
                            f"aşımına uğradı (deneme {attempt + 1}/{attempts}).",
                            flush=True,
                        )

                    if attempt + 1 < attempts:
                        delay = ACK_RETRY_DELAYS[min(
                            attempt,
                            len(ACK_RETRY_DELAYS) - 1,
                        )]
                        await page.wait_for_timeout(delay)

        print(f"[Yıldız] {brand} ürünleri yüklenemedi.", flush=True)
        return False

    async def _scrape_brand(self, page: Page, brand: str) -> list[dict]:
        items = []

        if not await self._load_brand_first_page(page, brand):
            return items

        for _ in range(MAX_PAGES):
            # Tablo satırlarını bekle
            try:
                await page.wait_for_selector(
                    "tbody#product-table-body tr",
                    timeout=30_000,
                )
            except Exception:
                break  # tablo gelmedi → bu marka bitti

            rows = page.locator("tbody#product-table-body tr")
            row_count = await rows.count()
            if row_count == 0:
                break

            for i in range(row_count):
                row = rows.nth(i)
                tds = row.locator("td")
                if await tds.count() < 8:
                    continue

                marka     = (await tds.nth(0).inner_text()).strip() or brand
                urun_kodu = (await tds.nth(1).inner_text()).strip()
                fiyat     = (await tds.nth(3).inner_text()).strip()
                iskonto   = (await tds.nth(4).inner_text()).strip()
                stok      = (await tds.nth(7).inner_text()).strip()

                # ACK genel arama yedeği açıklamasında "ack" geçen başka
                # markaları da döndürebilir; yalnız gerçek ACK satırlarını al.
                if brand.upper() == "ACK" and marka.upper() != "ACK":
                    continue

                min_paket = await row.evaluate(
                    """row => {
                        const clean = value => String(value ?? '').trim();
                        const fold = value => clean(value).toLocaleLowerCase('tr-TR');
                        const positiveValue = value => {
                            const text = clean(value);
                            const number = Number(text.replace(',', '.'));
                            return text && Number.isFinite(number) && number > 0
                                ? text
                                : '';
                        };

                        // Yıldız ürün listesinde minimum sipariş miktarı
                        // input[data-minorder] üzerinde tutuluyor.
                        const minOrderInput = row.querySelector('input[data-minorder]');
                        if (minOrderInput) {
                            const value = positiveValue(
                                minOrderInput.getAttribute('data-minorder')
                            );
                            if (value) return value;
                        }

                        // Aynı değer stok hücresindeki data-product JSON'unda da var.
                        const productElement = row.querySelector('.stock-cell[data-product]');
                        if (productElement) {
                            try {
                                const product = JSON.parse(
                                    productElement.getAttribute('data-product') || '{}'
                                );
                                const value = positiveValue(product.MIN_ORDER_QUANTITY);
                                if (value) return value;
                            } catch (_) {
                                // Bozuk/eksik JSON varsa diğer yedeklere devam et.
                            }
                        }

                        // Site ileride ayrı bir paket sütunu gösterirse onu da destekle.
                        const cells = [...row.querySelectorAll(':scope > td')];
                        const table = row.closest('table');
                        const headers = table
                            ? [...table.querySelectorAll('thead th')].map(th => fold(th.innerText))
                            : [];
                        const packageIndex = headers.findIndex(header =>
                            header.includes('paket') ||
                            header.includes('koli') ||
                            header.includes('minimum') ||
                            header.includes('min. miktar') ||
                            header.includes('min miktar')
                        );
                        if (packageIndex >= 0 && cells[packageIndex]) {
                            const text = clean(cells[packageIndex].innerText);
                            if (text) return text;
                        }

                        // Eski/alternatif HTML yapıları için genel yedekler.
                        const quantityInput = row.querySelector(
                            'input[type="number"], input[min], input[step]'
                        );
                        if (!quantityInput) return '';
                        for (const attribute of ['data-min-quantity', 'data-package-quantity', 'min', 'step']) {
                            const value = positiveValue(quantityInput.getAttribute(attribute));
                            if (value) return value;
                        }
                        return '';
                    }"""
                )

                if not urun_kodu and not marka:
                    continue

                items.append({
                    "brand":            marka or brand,
                    "product_code":     urun_kodu,
                    "stock":            stok,
                    "min_package_quantity": min_paket,
                    "discount_text":    iskonto,
                    "price_text":       fiyat,
                    "price":            _parse_price(fiyat),
                    "discount_percent": _parse_discount(iskonto),
                })

            # Sonraki sayfaya geç
            if not await _click_next(page):
                break

            await page.wait_for_timeout(PAGE_WAIT_MS)

        return items
