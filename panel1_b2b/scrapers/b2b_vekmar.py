"""
Vekmar B2B Scraper
URL    : https://b2b.vekmar.com.tr
Giriş  : Otomatik açılan DevExtreme LogOnPopup → e-posta + şifre
Akış   : Login → #vwSiparis → Marka lookup → SCHNEIDER seç →
         her satır için miktar 1..10 sırayla dene →
         "Mevcut ~Teslim" hücresi "X mevcut" diyorsa devam,
         başka bir şey yazıyorsa stok = son geçerli değer.
         10'a kadar "mevcut" yazıyorsa stok = 10.
         Sayfa sonunda "Sonraki" → tüm sayfalar bitene kadar.
Sütunlar: Marka | Ürün Kodu | Açıklama | Stok | Liste Fiyatı | Para Br.

NOT: Vekmar AJAX SPA, sayfa hash değiştiğinde session sıfırlanabilir.
Her sayfa geçişinden sonra LogOnPopup görünüyorsa tekrar giriş yapılır.
"""
import os
from pathlib import Path

from playwright.async_api import async_playwright, Page
from panel1_b2b.scrapers import BaseB2BScraper, register_scraper

os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(Path.home() / ".cache" / "ms-playwright"),
)

BASE_URL    = "https://b2b.vekmar.com.tr"
SIPARIS_URL = f"{BASE_URL}/#vwSiparis"

# Login form (gerçek HTML'e göre — popup içinde değil, inline table)
USER_SEL = "#txtKullaniciKodu input.dx-texteditor-input"
PASS_SEL = "#txtSifre input.dx-texteditor-input"
BTN_SEL  = "#btnGiris"

DEFAULT_BRANDS = ["SCHNEIDER"]
MAX_QTY_TRY    = 10
QTY_WAIT_MS    = 300    # miktar değişiminden sonra avail hücresi yenilenmesi
NAV_WAIT_MS    = 1500   # navigasyon sonrası AJAX yüklemesi için


@register_scraper("vekmar")
class VekmarScraper(BaseB2BScraper):
    COLUMNS = [
        ("brand",         "Marka",        18),
        ("product_code",  "Ürün Kodu",    22),
        ("product_name",  "Açıklama",     40),
        ("stock",         "Stok",         10),
        ("price_text",    "Liste Fiyatı", 14),
        ("currency",      "Para Br.",      8),
    ]

    async def scrape(self) -> list[dict]:
        products: list[dict] = []
        brands = self.brands if self.brands else DEFAULT_BRANDS

        async with async_playwright() as p:
            # "Target crashed" hatası /dev/shm küçük olduğunda olur →
            # --disable-dev-shm-usage ile /tmp kullanılır, crash önlenir.
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-extensions",
                ],
            )
            context = await browser.new_context(
                viewport={"width": 1600, "height": 1000}
            )

            try:
                page = await context.new_page()
                await _open_and_login(page, self.username, self.password)
                await _goto_siparis(page, self.username, self.password)

                for brand in brands:
                    await _select_brand(page, brand)
                    items = await _scrape_all_pages(page, brand)
                    products.extend(items)

            finally:
                await browser.close()

        return products


# ── LOGİN ─────────────────────────────────────────────────────────────────────
async def _open_and_login(page: Page, username: str, password: str):
    """Ana sayfa → login formu inline gelir (#txtKullaniciKodu, #txtSifre, #btnGiris)."""
    await page.goto(f"{BASE_URL}/", wait_until="domcontentloaded", timeout=30_000)
    # Şifre input'u görünene kadar bekle
    await page.wait_for_selector(PASS_SEL, timeout=30_000)
    await _fill_login(page, username, password)


async def _fill_login(page: Page, username: str, password: str):
    """Kullanıcı kodu + şifre doldur, Giriş butonuna bas."""
    user_inp = page.locator(USER_SEL).first
    pass_inp = page.locator(PASS_SEL).first

    await user_inp.wait_for(state="visible", timeout=15_000)
    await user_inp.fill("")
    await user_inp.fill(username)
    await pass_inp.fill("")
    await pass_inp.fill(password)

    # #btnGiris — "Şifremi Unuttum" butonuyla karışmasın diye ID kullanılır
    btn = page.locator(BTN_SEL).first
    await btn.wait_for(state="visible", timeout=15_000)
    await btn.evaluate("el => el.click();")

    # Şifre input'u sayfadan kalksın veya görünmez olsun (login başarılı)
    try:
        await page.wait_for_function(
            f"""() => {{
                const el = document.querySelector({PASS_SEL!r});
                if (!el) return true;
                return el.offsetParent === null;
            }}""",
            timeout=20_000,
        )
    except Exception:
        await page.wait_for_timeout(1500)


async def _ensure_logged_in(page: Page, username: str, password: str):
    """Şifre alanı tekrar görünürse session düşmüş → yeniden giriş yap."""
    inp = page.locator(PASS_SEL)
    if not await inp.count():
        return
    try:
        visible = await inp.first.is_visible()
    except Exception:
        visible = False
    if visible:
        await _fill_login(page, username, password)


# ── SİPARİŞ EKRANINA GİT ──────────────────────────────────────────────────────
async def _goto_siparis(page: Page, username: str, password: str):
    await page.goto(SIPARIS_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(NAV_WAIT_MS)
    await _ensure_logged_in(page, username, password)

    # Sipariş ekranındaki datagrid container'ı görünene kadar bekle
    await page.wait_for_selector(
        "div.dx-datagrid-rowsview", timeout=30_000
    )


# ── MARKA SEÇİMİ ──────────────────────────────────────────────────────────────
async def _select_brand(page: Page, brand: str):
    """
    Sayfadaki lookup/selectbox/dropdownEditor alanlarını sırayla dener.
    Açılan popup'ta marka adı varsa o lookup'tır → o item'a tıklar.
    """
    candidates = page.locator(
        "div.dx-lookup, div.dx-selectbox, div.dx-dropdowneditor"
    )

    cnt = await candidates.count()
    opened = False
    for i in range(cnt):
        try:
            el = candidates.nth(i)
            await el.scroll_into_view_if_needed()
            await el.click()
            # Brand popup açıldı mı?
            try:
                await page.wait_for_selector(
                    f"div.dx-overlay-content.dx-popup-normal "
                    f"div.dx-list-item:has-text('{brand}')",
                    timeout=2_500,
                )
                opened = True
                break
            except Exception:
                # Yanlış alan, popup'ı kapat → sıradakine geç
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
                await page.wait_for_timeout(200)
                continue
        except Exception:
            continue

    if not opened:
        raise Exception(
            f"Vekmar: '{brand}' markası için lookup popup'ı açılamadı"
        )

    # İlgili marka item'ına tıkla
    item = page.locator(
        f"div.dx-overlay-content.dx-popup-normal "
        f"div.dx-list-item:has-text('{brand}')"
    ).first
    await item.click()

    # Popup kapansın, grid yenilensin
    await page.wait_for_timeout(NAV_WAIT_MS)
    await page.wait_for_selector(
        "table.dx-datagrid-table tbody tr.dx-data-row",
        timeout=30_000,
    )


# ── TÜM SAYFALARI TARA ────────────────────────────────────────────────────────
async def _scrape_all_pages(page: Page, brand: str) -> list[dict]:
    items: list[dict] = []
    page_no = 0
    seen_first_codes: set[str] = set()

    while True:
        page_no += 1

        await page.wait_for_selector(
            "table.dx-datagrid-table tbody tr.dx-data-row",
            timeout=30_000,
        )

        # Aynı sayfa tekrar gelirse döngüden çık
        first_code = await _get_first_code(page)
        if first_code and first_code in seen_first_codes:
            break
        if first_code:
            seen_first_codes.add(first_code)

        page_items = await _scrape_current_page(page, brand)
        items.extend(page_items)

        # Sonraki sayfa butonu
        next_btn = page.locator(
            "div.dx-datagrid-pager div.dx-next-button"
        ).first
        if not await next_btn.count():
            break
        cls = (await next_btn.get_attribute("class")) or ""
        if "dx-button-disable" in cls or "dx-state-disabled" in cls:
            break

        await next_btn.click()

        # Sayfa değişimini doğrula (ilk satırın kodu değişti mi?)
        try:
            await page.wait_for_function(
                f"""() => {{
                    const td = document.querySelector(
                        'table.dx-datagrid-table tbody tr.dx-data-row td'
                    );
                    if (!td) return false;
                    return td.innerText.trim() !== {repr(first_code)};
                }}""",
                timeout=20_000,
            )
        except Exception:
            break

    return items


async def _get_first_code(page: Page) -> str:
    try:
        cell = page.locator(
            "table.dx-datagrid-table tbody tr.dx-data-row td"
        ).first
        if await cell.count():
            return (await cell.inner_text()).strip()
    except Exception:
        pass
    return ""


# ── TEK SAYFADAKİ SATIRLARI TARA ─────────────────────────────────────────────
async def _scrape_current_page(page: Page, brand: str) -> list[dict]:
    items = []
    row_count = await page.locator(
        "table.dx-datagrid-table tbody tr.dx-data-row"
    ).count()

    for i in range(row_count):
        try:
            # Stale element'e karşı her satır için yeniden locator
            row = page.locator(
                "table.dx-datagrid-table tbody tr.dx-data-row"
            ).nth(i)

            tds = row.locator("td")
            cnt = await tds.count()
            if cnt < 8:
                continue

            # Sabit alanları sadece bir kez oku
            code  = (await tds.nth(0).inner_text()).strip()
            desc  = (await tds.nth(1).inner_text()).strip()
            brnd  = (await tds.nth(2).inner_text()).strip() or brand
            price = (await tds.nth(6).inner_text()).strip()
            curr  = (await tds.nth(7).inner_text()).strip()

            stock = await _detect_stock_for_row(page, row)

            items.append({
                "brand":            brnd,
                "product_code":     code,
                "product_name":     desc,
                "stock":            str(stock),
                "price_text":       price,
                "currency":         curr,
            })
        except Exception:
            continue

    return items


# ── STOK TESPİTİ (hibrit binary search 1..10) ─────────────────────────────────
async def _detect_stock_for_row(page: Page, row) -> int:
    """
    Stok 0..10 arası tespit edilir. "stok ≥ n mi" monoton olduğu için
    binary search uygulanır (10 deneme yerine ~2-4 deneme):

      1) qty=1 dene → mevcut DEĞİL → stok 0 (çoğu ürün, tek deneme)
      2) qty=10 dene → mevcut → stok 10 (üst sınır, iki deneme)
      3) Aradaki değer → 2..9 binary search ile son "mevcut" değeri bul

    Davranış 1..10 sıralı denemeyle AYNI sonucu verir, sadece çok daha hızlı.
    """
    # 1) qty=1 ön eleme — stoksuz ürünleri tek denemede ele
    try:
        if not await _try_qty(page, row, 1):
            return 0
    except Exception:
        return 0

    # 2) qty=10 üst sınır — bol stoklu ürünleri iki denemede bitir
    try:
        if await _try_qty(page, row, MAX_QTY_TRY):
            return MAX_QTY_TRY
    except Exception:
        return 1  # 1 mevcuttu, 10 patladı → en az 1

    # 3) 1 mevcut, 10 değil → stok 2..9 arasında, binary search
    lo, hi, ans = 2, MAX_QTY_TRY - 1, 1
    while lo <= hi:
        mid = (lo + hi) // 2
        try:
            ok = await _try_qty(page, row, mid)
        except Exception:
            ok = False
        if ok:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


async def _try_qty(page: Page, row, qty: int) -> bool:
    """
    Miktar hücresine 'qty' yaz, Tab ile commit et,
    'Mevcut ~Teslim' hücresinde 'mevcut' geçiyor mu kontrol et.
    """
    miktar_td = row.locator("td").nth(3)
    avail_td  = row.locator("td").nth(5)

    try:
        await miktar_td.dblclick()
    except Exception:
        return False

    inp = miktar_td.locator("input").first
    try:
        await inp.wait_for(state="visible", timeout=3_000)
    except Exception:
        return False

    try:
        # fill() tek protokol çağrısı — Control+a/Delete/type'dan hızlı,
        # mevcut değeri temizleyip yeniyi yazar
        await inp.fill(str(qty))
        await inp.press("Tab")
    except Exception:
        return False

    # Avail hücresi yenilenmesi için bekle
    await page.wait_for_timeout(QTY_WAIT_MS)

    try:
        text = ((await avail_td.inner_text()) or "").strip().lower()
    except Exception:
        return False

    # 'X mevcut' → yeterli stok
    return "mevcut" in text
