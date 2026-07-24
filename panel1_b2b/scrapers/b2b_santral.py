"""
Santral B2B Scraper
URL    : https://www.santral.com
Giriş  : E-posta + Şifre (form içi)
Çekim  : Marka → (varsa) MenuGroup → Kategori ağacı (recursive)
         → Ürün listesi → her satıra 99900 yaz → "Stok Sorgula" → tabloyu oku
Sütunlar: Marka | Ürün Kodu | Stok | Birim Fiyat | İskonto

NOT: Site altyapısı zayıf — orijinal C# kodundaki tüm Thread.Sleep ve
WebDriverWait timeout değerleri BİREBİR KORUNMUŞTUR.
Stok değerleri tek bir "stock" alanında TOPLANIR (Karaköy + Perpa + diğer).
"""
import os
import re
from pathlib import Path

from playwright.async_api import async_playwright, Page
from panel1_b2b.scrapers import BaseB2BScraper, register_scraper

os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(Path.home() / ".cache" / "ms-playwright"),
)

BASE_URL = "https://www.santral.com"
HOME_URL = BASE_URL.rstrip("/").lower()

# Marka listesi — kondas/makel/metop/recber kullanıcı isteği üzerine çıkarıldı
ALL_BRANDS = [
    "entes", "erse", "klemsan",
    "molwex", "nexans", "oznur", "prysmian", "schrack",
    "siemens-salt-ve-enstalasyon-urunleri", "simpa",
]


@register_scraper("santral")
class SantralScraper(BaseB2BScraper):
    COLUMNS = [
        ("brand",         "Marka",       20),
        ("product_code",  "Ürün Kodu",   28),
        ("stock",         "Stok",        14),
        ("min_package_qty", "Min. Paket Adedi", 18),
        ("price_text",    "Liste Fiyatı", 16),
        ("discount_text", "İskonto",     12),
    ]

    async def scrape(self) -> list[dict]:
        products: list[dict] = []
        brands = self.brands if self.brands else ALL_BRANDS

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(viewport={"width": 1400, "height": 900})

            try:
                page = await context.new_page()
                await _do_login(page, self.username, self.password)

                for brand in brands:
                    brand_slug = brand.strip().strip("/")
                    items = await _scrape_brand(page, brand_slug)
                    products.extend(items)

            finally:
                await browser.close()

        return products


# ── Giriş ─────────────────────────────────────────────────────────────────────
async def _do_login(page: Page, username: str, password: str):
    # 1) Ana sayfa
    await page.goto(f"{BASE_URL}/", wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_function(
        "document.readyState === 'complete'", timeout=30_000
    )

    # 2) "GİRİŞ YAP" butonu (JS click — overlay/aspect sorunlarını aşar)
    login_btn = page.locator(
        "div.header-member-wrapper-inline a.link-btn.login.med"
    ).first
    await login_btn.wait_for(state="attached", timeout=15_000)
    await login_btn.evaluate("el => el.click();")

    # 3) Login formu açıldı mı?
    await page.wait_for_selector(
        "div.form-wrapper.login.opened", timeout=15_000
    )

    # 4) Inputlar
    user_input = page.locator("#login-form-username")
    await user_input.wait_for(state="visible", timeout=15_000)
    await user_input.fill("")
    await user_input.fill(username)

    pass_input = page.locator("#login-form-password")
    await pass_input.fill("")
    await pass_input.fill(password)

    # 5) Submit
    submit_btn = page.locator(
        "form#header-login-form button.form-button[type='submit']"
    ).first
    await submit_btn.wait_for(state="attached", timeout=15_000)
    await submit_btn.evaluate("el => el.click();")

    # 6) Form "opened" sınıfı kalkar mı? Bazı temalarda kalkmaz → 1500ms sleep
    try:
        await page.wait_for_function(
            "document.querySelectorAll('div.form-wrapper.login.opened').length === 0",
            timeout=15_000,
        )
    except Exception:
        await page.wait_for_timeout(1500)


# ── Marka çekimi ──────────────────────────────────────────────────────────────
async def _scrape_brand(page: Page, brand_slug: str) -> list[dict]:
    items: list[dict] = []
    brand_url = f"{BASE_URL}/{brand_slug}"

    await page.goto(brand_url, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_function(
        "document.readyState === 'complete'", timeout=30_000
    )

    # #brand-nav-menu altında iki yapı olabilir:
    #   Yapı 1: ilk v3-brand-nav sadece "Favori Ürünleriniz" içerir
    #   Yapı 2: ilk v3-brand-nav'da MenuGroup linkleri var (Klemsan / Siemens)
    menu_groups = await _get_menu_groups(page)

    if menu_groups:
        # Yapı 2: her MenuGroup için aç → kategori ağacını recursive gez
        for mg_url in menu_groups:
            await page.goto(mg_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_function(
                "document.readyState === 'complete'", timeout=30_000
            )
            cat_urls = await _collect_category_urls(page)
            for cat_url in cat_urls:
                items.extend(await _scrape_category(page, cat_url))
    else:
        # Yapı 1: doğrudan kategori ağacı
        cat_urls = await _collect_category_urls(page)
        for cat_url in cat_urls:
            items.extend(await _scrape_category(page, cat_url))

    return items


# ── MenuGroup tespiti (Yapı 2) ────────────────────────────────────────────────
async def _get_menu_groups(page: Page) -> list[str]:
    """
    İlk #brand-nav-menu > div.v3-brand-nav (visible-lg/sm sınıfları OLMAYAN)
    içinde "Favori Ürünleriniz" dışında MenuGroup linkleri varsa onları döndürür.
    Aksi halde boş liste → Yapı 1.
    """
    try:
        await page.wait_for_selector("#brand-nav-menu", timeout=15_000)
    except Exception:
        return []

    first_div = page.locator(
        "#brand-nav-menu > div.v3-brand-nav:not(.visible-lg):not(.visible-sm)"
    ).first
    if not await first_div.count():
        return []

    links = await first_div.locator("a[href]").evaluate_all(
        "els => els.map(e => ({href: e.href, text: (e.innerText||'').trim()}))"
    )

    groups: list[str] = []
    seen: set[str] = set()
    for it in links:
        href = (it.get("href") or "").strip()
        text = (it.get("text") or "").strip().lower()
        if not href:
            continue
        # "Favori Ürünleriniz" linkini atla
        if "favori" in text:
            continue
        # MenuGroup linki mi? (?MenuGroup=XX)
        if "menugroup=" not in href.lower():
            continue
        if href in seen:
            continue
        seen.add(href)
        groups.append(href)

    return groups


# ── Kategori ağacındaki SADECE EN ÜST SEVİYE linkleri topla ──────────────────
async def _collect_category_urls(page: Page) -> list[str]:
    """
    div.v3-brand-nav.visible-lg.visible-sm > ul.multi-level-menu içindeki
    SADECE EN ÜST SEVİYE (data-level='0') a.menu-link href'lerini döndürür.
    Üst kategoriye gidildiğinde alt kategorilerin ürünleri de birlikte
    listelendiği için alt seviyeleri gezmeye gerek yok (zaman tasarrufu).
    """
    try:
        await page.wait_for_selector(
            "div.v3-brand-nav.visible-lg.visible-sm > ul.multi-level-menu",
            timeout=15_000,
        )
    except Exception:
        return []

    hrefs = await page.locator(
        "div.v3-brand-nav.visible-lg.visible-sm "
        "> ul.multi-level-menu > li.menu-item[data-level='0'] > a.menu-link[href]"
    ).evaluate_all(
        "els => els.map(e => e.href).filter(h => h && h.trim().length > 0)"
    )

    seen: set[str] = set()
    unique: list[str] = []
    for h in hrefs:
        if h not in seen:
            seen.add(h)
            unique.append(h)
    return unique


# ── Tek kategori sayfası: stok sorgula + tabloyu oku ──────────────────────────
async def _scrape_category(page: Page, cat_url: str) -> list[dict]:
    await page.goto(cat_url, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_function(
        "document.readyState === 'complete'", timeout=30_000
    )

    # Anasayfaya redirect → kategori yok, atla
    cur = (page.url or "").rstrip("/").lower()
    if cur == HOME_URL or cur == (HOME_URL + "/"):
        return []

    # Ürün tablosu gerçekten var mı? (kısa wait — yoksa kategori sayfası değildir)
    try:
        await page.wait_for_selector(
            "tbody#bodyProducts tr", timeout=5_000
        )
    except Exception:
        return []

    # Tüm satırlara 99900 yaz (sanal scroll'lu tbody için satır satır gez)
    await _fill_amount_inputs(page)

    # "Stok Sorgula" butonuna bas
    try:
        stok_btn = page.locator("button.get-stock").first
        await stok_btn.wait_for(state="attached", timeout=15_000)
        await stok_btn.evaluate("el => el.scrollIntoView({block:'center'});")
        await page.wait_for_timeout(200)
        await stok_btn.evaluate("el => el.click();")
        await page.wait_for_timeout(200)
    except Exception:
        return []

    return await _read_table_rows(page)


# ── Tbody'deki her satıra 99900 yaz (C# scroll mantığıyla birebir) ────────────
async def _fill_amount_inputs(page: Page):
    tbody = page.locator("tbody#bodyProducts").first
    if not await tbody.count():
        return

    i = 0
    last_stable_count = 0
    stable_hit = 0

    while True:
        rows = page.locator("tbody#bodyProducts tr")
        count = await rows.count()

        if count == last_stable_count:
            stable_hit += 1
        else:
            stable_hit = 0
            last_stable_count = count

        # Satır bitti ve yeni gelmiyor → çık
        if i >= count and stable_hit >= 6:
            break

        # i mevcut count'u geçtiyse aşağı kaydır → yeni satırlar yüklensin
        if i >= count:
            await tbody.evaluate(
                "el => { el.scrollTop = el.scrollTop + el.clientHeight; }"
            )
            continue

        try:
            row = rows.nth(i)
            await row.evaluate("el => el.scrollIntoView({block:'center'});")

            inp = row.locator("td.amount input.table-input").first
            if not await inp.count():
                i += 1
                continue

            # HIZLANDIRMA: click+ctrl+a+backspace+type 5 protokol turu yerine
            # tek fill() ile değer set + input event dispatch. Davranış aynı.
            await inp.fill("99900")
            await inp.press("Tab")  # blur → değer commit

            # KRİTİK: 1 satır kadar aşağı kaydır (C# ile birebir)
            row_height = await row.evaluate(
                "el => el.getBoundingClientRect().height || 40"
            )
            await tbody.evaluate(
                f"el => {{ el.scrollTop = el.scrollTop + {row_height}; }}"
            )

            i += 1
        except Exception:
            # Stale element veya tek satır hatası → akışı bozma
            i += 1
            continue


# ── Sorgulanmış tablo satırlarını oku ─────────────────────────────────────────
async def _read_table_rows(page: Page) -> list[dict]:
    items: list[dict] = []
    rows = await page.locator("#bodyProducts tr.product-item").all()

    for tr in rows:
        try:
            # Marka
            try:
                el = tr.locator("td.product-name .brand span").first
                marka = (await el.inner_text()).strip() if await el.count() else "0"
            except Exception:
                marka = "0"

            # Ürün Kodu
            try:
                el = tr.locator("td.product-name a.product-code").first
                urun_kodu = (await el.inner_text()).strip() if await el.count() else "0"
            except Exception:
                urun_kodu = "0"

            # Stok — TÜM lokasyonların toplamı (Karaköy + Perpa + diğer ilçeler)
            total_stock = 0
            try:
                spans = await tr.locator("td.stock-info p.stok-sonuc span").all()
                for sp in spans:
                    t = (await sp.inner_text()).strip()
                    total_stock += _extract_first_int(t)
            except Exception:
                total_stock = 0

            # Min. Paket Adedi — satırdaki "Kafile" adedi
            try:
                min_package_qty = (
                    (await tr.get_attribute("data-ktad")) or ""
                ).strip()
                if not min_package_qty:
                    amount_input = tr.locator("td.amount input.table-input").first
                    if await amount_input.count():
                        min_package_qty = (
                            (await amount_input.get_attribute("data-ktad")) or ""
                        ).strip()
            except Exception:
                min_package_qty = ""

            # Liste Fiyatı
            try:
                el = tr.locator("td.list-price .price.pr-first").first
                price_text = (await el.inner_text()).strip() if await el.count() else ""
            except Exception:
                price_text = ""

            # İskonto — önce .dusuk-iskonto, yoksa ilk .isk-value
            disc_text = ""
            try:
                disc_td = tr.locator("td.discount-value").first
                if await disc_td.count():
                    dusuk = disc_td.locator(".dusuk-iskonto")
                    if await dusuk.count():
                        disc_text = (await dusuk.first.inner_text()).strip()
                    else:
                        normal = disc_td.locator(".isk-value")
                        if await normal.count():
                            disc_text = (await normal.first.inner_text()).strip()
            except Exception:
                disc_text = ""

            items.append({
                "brand":            marka or "0",
                "product_code":     urun_kodu or "0",
                "stock":            str(total_stock),
                "min_package_qty":  min_package_qty,
                "price_text":       price_text,
                "price":            _parse_tr_decimal(price_text),
                "discount_text":    disc_text,
                "discount_percent": _parse_tr_decimal(disc_text),
            })
        except Exception:
            # tek satır patlarsa devam (C# ile aynı)
            continue

    return items


# ── Yardımcılar (C# ExtractFirstInt / ExtractFirstDecimalTR karşılığı) ───────
def _extract_first_int(text: str) -> int:
    if not text:
        return 0
    m = re.search(r"(\d+)", text)
    if not m:
        return 0
    try:
        return int(m.group(1))
    except ValueError:
        return 0


def _parse_tr_decimal(text: str) -> float | None:
    """'8,32 TRL' → 8.32 ; '9.105,60 TRL' → 9105.60"""
    if not text:
        return None
    m = re.search(r"\d{1,3}(\.\d{3})*(,\d+)?|\d+(,\d+)?", text)
    if not m:
        return None
    s = m.group(0).strip()
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
