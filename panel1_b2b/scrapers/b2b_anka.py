"""
Anka B2B Scraper
URL     : https://b2b.ankamuh.com
Giriş   : E-posta + Şifre
Çekim   : Chosen.js marka filtresi + DataTables sayfalama
Sütunlar: Marka | Ürün Kodu | Stok | İskonto | Liste Fiyat
"""
import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright, Page
from panel1_b2b.scrapers import BaseB2BScraper, register_scraper

os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(Path.home() / ".cache" / "ms-playwright"),
)

LOGIN_URL    = "https://b2b.ankamuh.com/auth/login"
PRODUCTS_URL = "https://b2b.ankamuh.com/products"
SESSION_STATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / ".sessions"
    / "anka_storage_state.json"
)


@register_scraper("anka")
class AnkaScraper(BaseB2BScraper):
    COLUMNS = [
        ("brand",         "Marka",       20),
        ("product_code",  "Ürün Kodu",   20),
        ("stock",         "Stok",        15),
        ("min_package_qty", "Min. Paket Adedi", 18),
        ("discount_text", "İskonto",     12),
        ("price_text",    "Liste Fiyat", 16),
    ]

    async def scrape(self) -> list[dict]:
        products = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await _new_context(browser)
            authenticated = False

            try:
                page = await context.new_page()

                # Anka ayni hesapla kisa aralikli yeni girisleri zaman zaman
                # kabul etmiyor. Once onceki basarili oturumu kullanmayi dene.
                authenticated = await _restore_session(page)
                if not authenticated:
                    await _do_login(page, self.username, self.password)

                # Ürünler sayfasına git + 15sn bekle (C# ile aynı)
                await page.goto(PRODUCTS_URL, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_function(
                    "document.readyState === 'complete'", timeout=30_000
                )
                if "/auth/login" in page.url:
                    raise Exception("Anka oturumu urunler sayfasinda dogrulanamadi")

                authenticated = True
                await _save_session(context)
                await page.wait_for_timeout(15_000)

                # Sayfa başına 100 kayıt seç (daha az sayfa)
                await _set_page_size(page, "100")

                # Marka listesini al
                all_brands = await _get_brand_options(page)

                # settings'ten gelen marka listesi varsa filtrele
                if self.brands:
                    normalized = {b.strip().lower() for b in self.brands}
                    selected = [
                        (val, txt) for val, txt in all_brands
                        if txt.lower() in normalized or val.lower() in normalized
                    ]
                else:
                    selected = all_brands  # hepsi

                for brand_val, brand_txt in selected:
                    items = await _scrape_brand(page, brand_val, brand_txt)
                    products.extend(items)

            finally:
                # Cekim sonraki asamalarda hata verse bile gecerli oturumu koru.
                if authenticated:
                    await _save_session(context)
                await browser.close()

        return products


# ── Giriş ─────────────────────────────────────────────────────────────────────
async def _new_context(browser):
    """Kayitli Anka oturumu varsa onunla, bozuksa temiz context ile basla."""
    kwargs = {"viewport": {"width": 1400, "height": 900}}
    if SESSION_STATE_PATH.exists():
        try:
            return await browser.new_context(
                **kwargs, storage_state=str(SESSION_STATE_PATH)
            )
        except Exception:
            pass
    return await browser.new_context(**kwargs)


async def _restore_session(page: Page) -> bool:
    """Yeni login gondermeden kayitli oturumun halen gecerli oldugunu dogrula."""
    if not SESSION_STATE_PATH.exists():
        return False
    try:
        await page.goto(PRODUCTS_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_function(
            "document.readyState === 'complete'", timeout=30_000
        )
        return "/auth/login" not in page.url
    except Exception:
        return False


async def _save_session(context):
    """Basarili girisin cookie/localStorage bilgisini sonraki cekime sakla."""
    try:
        SESSION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(SESSION_STATE_PATH))
    except Exception:
        # Oturumun diske yazilamamasi mevcut cekimi basarisiz saydirmasin.
        pass


async def _login_error_text(page: Page) -> str:
    """Login sayfasinin gosterdigi gercek hata/uyari metnini bul."""
    selectors = (
        ".alert:visible",
        ".uk-alert:visible",
        ".invalid-feedback:visible",
        ".text-danger:visible",
        ".swal2-popup:visible",
        "[role='alert']:visible",
    )
    for selector in selectors:
        try:
            messages = page.locator(selector)
            for index in range(min(await messages.count(), 5)):
                text = " ".join((await messages.nth(index).inner_text()).split())
                if text:
                    return text[:300]
        except Exception:
            continue
    return "Giris formu gonderildi ancak yonlenme olmadi"


async def _do_login(page: Page, username: str, password: str):
    """Anka login — kararsız olabildiği için 3 denemeli + başarı doğrulamalı."""
    last_err = None
    for attempt in range(1, 4):
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_function("document.readyState === 'complete'", timeout=30_000)

            # Sayfada birden fazla form/input var (5 element) → görünür olanı seç,
            # .first ile strict mode violation önlenir
            email_el = page.locator("form.form input[name='email']:visible").first
            pass_el  = page.locator("form.form input[name='password']:visible").first

            await email_el.wait_for(state="visible", timeout=20_000)
            await email_el.fill(username)
            await pass_el.fill(password)

            login_form = page.locator("form.form:visible").first
            # Anka'nin KT on-yuz validator'u gecerli ve dolu adresi zaman zaman
            # "gecersiz e-posta" diye engelliyor. Formu native submit ile
            # gonderince CSRF + kullanici/sifre sunucuda normal dogrulanmaya
            # devam eder; yalnizca hatali tarayici validator'u devre disi kalir.
            await login_form.evaluate(
                "form => HTMLFormElement.prototype.submit.call(form)"
            )

            # Login BAŞARISINI doğrula: /auth/login'den çıkmalı (yönlenme olmalı)
            try:
                await page.wait_for_function(
                    "() => !window.location.href.includes('/auth/login')",
                    timeout=15_000,
                )
                await page.wait_for_function(
                    "document.readyState === 'complete'", timeout=15_000
                )
                return  # başarılı
            except Exception:
                last_err = await _login_error_text(page)
                await asyncio.sleep(5)  # sunucuya art arda yuklenme yapma
                continue
        except Exception as e:
            last_err = str(e)
            if page.is_closed():
                break
            await asyncio.sleep(2)
            continue

    raise Exception(f"Anka login başarısız ({last_err})")


# ── select#filterBrand'dan tüm markaları al ───────────────────────────────────
async def _get_brand_options(page: Page) -> list[tuple[str, str]]:
    """Returns list of (value, display_text)."""
    # Chosen.js orijinal <select>'i GİZLER (display:none) ve kendi UI'sını
    # gösterir → state="attached" ile bekle, "visible" değil
    await page.wait_for_selector("#filterBrand", state="attached", timeout=30_000)
    # Chosen.js ile seçenek sayısının > 1 olmasını bekle
    await page.wait_for_function(
        "document.querySelectorAll('#filterBrand option').length > 1",
        timeout=30_000,
    )
    options = await page.locator("#filterBrand option").all()
    brands = []
    for opt in options:
        val = (await opt.get_attribute("value") or "").strip()
        txt = (await opt.inner_text()).strip()
        if val:
            brands.append((val, txt))
    return brands


# ── Marka seç (Chosen.js) ─────────────────────────────────────────────────────
async def _select_brand(page: Page, value: str, brand_text: str):
    """jQuery Chosen dropdown'dan marka seç, tablo yenilenmesini bekle."""

    # Mevcut ilk hücreyi kaydet
    before = ""
    try:
        first_td = page.locator("table#productTable tbody tr td").first
        if await first_td.count():
            before = (await first_td.inner_text()).strip()
    except Exception:
        pass

    # C# kodundaki JS ile birebir aynı
    await page.evaluate(
        """(val) => {
            const s = document.getElementById('filterBrand');
            if (!s) return;
            s.value = val;
            s.dispatchEvent(new Event('change', {bubbles: true}));
            if (window.jQuery) {
                jQuery(s).val(val).trigger('change').trigger('chosen:updated');
            }
        }""",
        value,
    )

    # Tablo değişimini bekle
    try:
        await page.wait_for_function(
            f"""() => {{
                const tds = document.querySelectorAll('table#productTable tbody tr td');
                if (!tds.length) return false;
                return tds[0].innerText.trim() !== {repr(before)};
            }}""",
            timeout=20_000,
        )
    except Exception:
        pass

    await _wait_table(page)


# ── DataTables yüklenmesini bekle ─────────────────────────────────────────────
async def _wait_table(page: Page):
    try:
        await page.wait_for_function(
            """() => {
                const pr = document.querySelector('#productTable_processing');
                if (pr) {
                    const s = (pr.getAttribute('style') || '');
                    if (s.includes('block') && !s.includes('none')) return false;
                }
                return document.querySelectorAll('table#productTable tbody tr').length > 0;
            }""",
            timeout=20_000,
        )
    except Exception:
        pass


# ── Sayfa başına 100 kayıt ────────────────────────────────────────────────────
async def _set_page_size(page: Page, size: str = "100"):
    try:
        sel = page.locator("select[name='productTable_length']")
        if await sel.count():
            await sel.select_option(size)
            await _wait_table(page)
    except Exception:
        pass


# ── Next butonu ───────────────────────────────────────────────────────────────
async def _is_next_enabled(page: Page) -> bool:
    el = page.locator("div.dt-paging ul.uk-pagination li a.next").first
    if not await el.count():
        return False
    cls = (await el.get_attribute("class")) or ""
    return "uk-disabled" not in cls


async def _click_next(page: Page):
    el = page.locator("div.dt-paging ul.uk-pagination li a.next").first
    await el.evaluate("el => el.click()")


# ── Marka için tüm sayfaları tara ────────────────────────────────────────────
async def _scrape_brand(page: Page, brand_val: str, brand_txt: str) -> list[dict]:
    await _select_brand(page, brand_val, brand_txt)

    items = []
    while True:
        await _wait_table(page)
        page_items = await _read_table_page(page)
        items.extend(page_items)

        if not await _is_next_enabled(page):
            break

        await _click_next(page)

    return items


# ── Mevcut sayfa satırlarını oku ──────────────────────────────────────────────
async def _read_table_page(page: Page) -> list[dict]:
    items = []
    rows = await page.locator("table#productTable tbody tr").all()

    for row in rows:
        tds = row.locator("td")
        cnt = await tds.count()
        if cnt < 10:
            continue
        try:
            # [0]=Marka [1]=ÜrünKodu [3]=Stok [6]=Fiyat [7]=İskonto
            # [9]=Min. Sipariş Adedi
            brand     = (await tds.nth(0).inner_text()).strip()
            urun_kodu = (await tds.nth(1).inner_text()).strip()
            stok      = (await tds.nth(3).inner_text()).strip()
            fiyat     = (await tds.nth(6).inner_text()).strip()
            iskonto   = (await tds.nth(7).inner_text()).strip()
            min_package_qty = (await tds.nth(9).inner_text()).strip()

            items.append({
                "brand":         brand,
                "product_code":  urun_kodu,
                "stock":         stok,
                "min_package_qty": min_package_qty,
                "price_text":    fiyat,
                "discount_text": iskonto,
            })
        except Exception:
            continue

    return items
