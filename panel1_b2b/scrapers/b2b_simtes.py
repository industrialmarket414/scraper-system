"""Simtes B2B tekliflerinden güncel fiyat ve iskonto bilgilerini çeker.

Simtes portalı genel bir ürün listeleme ekranı sunmuyor. Fiyat, iskonto ve
stok bilgileri müşterinin taslak tekliflerinin ürün ekranlarında gösteriliyor.
Yapılandırılmış marka tekliflerine eksik ürün kodları 20'li gruplarla eklenir;
ardından liste fiyatı, iskonto, net fiyat ve doğrulanan stok adedi okunur.
"""

import os
import re
from pathlib import Path

from playwright.async_api import Page, async_playwright

from panel1_b2b.scrapers import BaseB2BScraper, register_scraper


os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(Path.home() / ".cache" / "ms-playwright"),
)

BASE_URL = "https://portal.simteselektrik.com.tr"
LOGIN_URL = f"{BASE_URL}/login"
DRAFTS_URL = f"{BASE_URL}/company/my-offers/my-index"
DEFAULT_MANUAL_LOGIN_TIMEOUT_SECONDS = 300
CHROME_PATHS = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)
SESSION_STATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / ".sessions"
    / "simtes_storage_state.json"
)
PERSISTENT_PROFILE_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / ".sessions"
    / "simtes_chrome_profile"
)
DEFAULT_PRODUCT_CODES_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "sources"
    / "simtes_schneider_codes.txt"
)
DEFAULT_CHECKED_CODES_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "sources"
    / "simtes_schneider_checked_codes.txt"
)


@register_scraper("simtes")
class SimtesScraper(BaseB2BScraper):
    COLUMNS = [
        ("product_code", "Ürün Kodu", 22),
        ("product_name", "Ürün Açıklaması", 45),
        ("quantity", "Teklif Adedi", 14),
        ("list_price", "Liste Fiyatı", 16),
        ("discount", "İskonto %", 12),
        ("net_price", "Net Fiyat", 16),
        ("currency", "Para Br.", 10),
        ("stock", "Stok Adedi", 14),
        ("stock_status", "Stok Durumu", 14),
        ("delivery_date", "Teslim Tarihi", 15),
        ("offer_date", "Teklif Tarihi", 15),
        ("offer_code", "Teklif Kodu", 24),
        ("payment_type", "Ödeme Türü", 20),
    ]

    def __init__(self, site_config: dict):
        super().__init__(site_config)
        configured_path = site_config.get("product_codes_file", "")
        self.product_codes_path = (
            Path(configured_path) if configured_path else DEFAULT_PRODUCT_CODES_PATH
        )
        self.product_code_files = {
            str(brand).casefold(): Path(path)
            for brand, path in (
                site_config.get("product_code_files", {}) or {}
            ).items()
            if path
        }
        configured_checked_path = site_config.get("checked_product_codes_file", "")
        self.checked_product_codes_path = (
            Path(configured_checked_path)
            if configured_checked_path
            else DEFAULT_CHECKED_CODES_PATH
        )
        self.checked_product_code_files = {
            str(brand).casefold(): Path(path)
            for brand, path in (
                site_config.get("checked_product_code_files", {}) or {}
            ).items()
            if path
        }
        self.offer_urls = {
            str(brand).casefold(): str(url)
            for brand, url in (site_config.get("offer_urls", {}) or {}).items()
            if url
        }
        self.populate_missing_products = bool(
            site_config.get("populate_missing_products", False)
        )
        self.populate_batch_size = min(
            20, max(1, int(site_config.get("populate_batch_size", 20)))
        )
        self.request_quantity = max(
            1, int(site_config.get("request_quantity", 100))
        )
        self.manual_login_required = (
            site_config.get("_trigger_type") == "manual"
            and bool(site_config.get("manual_login_on_manual_run", True))
        )
        self.manual_login_timeout_ms = max(
            60,
            int(site_config.get(
                "manual_login_timeout_seconds",
                DEFAULT_MANUAL_LOGIN_TIMEOUT_SECONDS,
            )),
        ) * 1000

    def product_codes(self, brand: str | None = None) -> list[str]:
        """Markanın Simtes sorgu kodlarını; varsayılan olarak Schneider'ı döndür."""
        path = self.product_codes_path
        if brand:
            path = self.product_code_files.get(brand.casefold(), path)
        return _load_product_codes(path)

    def pending_product_codes(self, brand: str | None = None) -> list[str]:
        """Daha önce Simtes'te kontrol edilmemiş kodları sıra koruyarak döndür."""
        checked_path = self.checked_product_codes_path
        if brand:
            checked_path = self.checked_product_code_files.get(
                brand.casefold(), checked_path
            )
        checked = {
            code.casefold()
            for code in _load_product_codes(checked_path)
        }
        product_codes = self.product_codes(brand) if brand else self.product_codes()
        return [
            code
            for code in product_codes
            if code.casefold() not in checked
        ]

    def offer_url(self, brand: str) -> str:
        """Marka için ayrılmış 90 günlük Simtes taslağının adresini döndür."""
        return self.offer_urls.get(brand.casefold(), "")

    def checked_codes_path(self, brand: str | None = None) -> Path:
        path = self.checked_product_codes_path
        if brand:
            path = self.checked_product_code_files.get(brand.casefold(), path)
        return path

    def mark_checked(self, brand: str, codes) -> None:
        """Başarıyla sorgulanan kodları yeniden eklenmeyecek şekilde kaydet."""
        path = self.checked_codes_path(brand)
        existing = []
        if path.exists():
            existing = _normalize_product_codes(
                path.read_text(encoding="utf-8-sig").splitlines()
            )
        merged = _normalize_product_codes([*existing, *codes])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(merged) + ("\n" if merged else ""), encoding="utf-8")

    async def scrape(self) -> list[dict]:
        if not self.username or not self.password:
            raise RuntimeError("Simtes kullanıcı adı veya şifresi eksik")

        async with async_playwright() as playwright:
            context = await _launch_context(playwright)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                if self.manual_login_required:
                    await _manual_login(
                        page,
                        timeout_ms=self.manual_login_timeout_ms,
                    )
                elif not await _restore_session(page):
                    await _manual_login(
                        page,
                        timeout_ms=self.manual_login_timeout_ms,
                    )
                await _save_session(context)

                if self.populate_missing_products:
                    for brand in self.brands:
                        url = self.offer_url(brand)
                        if url:
                            await _populate_offer_codes(page, self, brand, url)

                draft_links = await _read_draft_links(page)
                if self.offer_urls:
                    configured = set(self.offer_urls.values())
                    draft_links = [
                        draft for draft in draft_links
                        if draft["url"] in configured
                    ]
                if not draft_links:
                    raise RuntimeError("Simtes portalında okunabilecek taslak teklif bulunamadı")

                products: list[dict] = []
                for draft in draft_links:
                    await page.goto(
                        draft["url"], wait_until="domcontentloaded", timeout=45_000
                    )
                    if "/login" in page.url:
                        raise RuntimeError("Simtes oturumu teklif okunurken sona erdi")
                    products.extend(await _read_offer_products(page, draft))

                await _save_session(context)
                return _prefer_latest_product_rows(products)
            finally:
                await context.close()


async def _populate_offer_codes(
    page: Page,
    scraper: SimtesScraper,
    brand: str,
    offer_url: str,
) -> None:
    """Eksik marka kodlarını mevcut taslağa, portal sınırı olan 20'li ekler."""
    await page.goto(offer_url, wait_until="domcontentloaded", timeout=45_000)
    if "/login" in page.url:
        raise RuntimeError("Simtes oturumu ürünler eklenirken sona erdi")

    # Önce teklifte zaten bulunanları işaretlemek yeniden çalıştırmayı güvenli
    # kılar; önceki çalışma kaydetmeden kesilmiş olsa bile miktar katlanmaz.
    existing_rows = await _read_offer_products(page, {
        "offer_date": "",
        "offer_code": "",
        "payment_type": "",
    })
    target_keys = {code.casefold() for code in scraper.product_codes(brand)}
    scraper.mark_checked(brand, [
        row["product_code"] for row in existing_rows
        if row["product_code"].casefold() in target_keys
    ])

    pending = scraper.pending_product_codes(brand)
    bulk_url = _offer_bulk_url(offer_url)
    for offset in range(0, len(pending), scraper.populate_batch_size):
        batch = pending[offset:offset + scraper.populate_batch_size]
        await page.goto(bulk_url, wait_until="domcontentloaded", timeout=45_000)
        codes = page.locator('textarea[name="productCodes"]')
        quantities = page.locator('textarea[name="quantities"]')
        await codes.wait_for(state="visible", timeout=30_000)
        await codes.fill("\n".join(batch))
        await quantities.fill("\n".join(
            [str(scraper.request_quantity)] * len(batch)
        ))
        await page.locator('#offerDraftFromExcel input[type="submit"]').click()
        await page.wait_for_url(
            re.compile(r"/company/my-offers/add-product-to-offer/"),
            timeout=90_000,
        )
        alert = _clean(await page.locator(".alert:visible").first.inner_text())
        if "ürün bulundu" not in alert.casefold():
            raise RuntimeError(
                f"Simtes {brand} toplu ürün ekleme sonucu doğrulanamadı: {alert}"
            )
        scraper.mark_checked(brand, batch)
        print(
            f"Simtes {brand}: {min(offset + len(batch), len(pending))}/"
            f"{len(pending)} kod kontrol edildi"
        )


def _offer_bulk_url(offer_url: str) -> str:
    return offer_url.replace("/add-product-to-offer/", "/offer-from-excel/")


async def _launch_context(playwright):
    """Cloudflare güvenini koruyan, Simtes'e özel kalıcı Chrome profili aç."""
    chrome = next((path for path in CHROME_PATHS if path.exists()), None)
    PERSISTENT_PROFILE_PATH.mkdir(parents=True, exist_ok=True)
    kwargs = {
        "headless": False,
        "viewport": {"width": 1400, "height": 900},
    }
    if chrome:
        kwargs["executable_path"] = str(chrome)
    return await playwright.chromium.launch_persistent_context(
        str(PERSISTENT_PROFILE_PATH), **kwargs
    )


async def _restore_session(page: Page) -> bool:
    try:
        await page.goto(DRAFTS_URL, wait_until="domcontentloaded", timeout=45_000)
        if "/login" in page.url:
            return False
        try:
            await page.locator("#kt_table_user").wait_for(
                state="attached", timeout=20_000
            )
        except Exception:
            return False
        return "/company/" in page.url
    except Exception:
        return False


async def _save_session(context) -> None:
    try:
        SESSION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(SESSION_STATE_PATH))
    except Exception:
        pass


async def _manual_login(
    page: Page,
    timeout_ms: int = DEFAULT_MANUAL_LOGIN_TIMEOUT_SECONDS * 1000,
) -> None:
    """Siteyi açıp CAPTCHA, bilgiler ve giriş tıklamasını tamamen kullanıcıya bırak."""
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
    if "/company/" in page.url:
        return

    print(
        "Simtes manuel giriş bekleniyor: Açılan Chrome penceresinde CAPTCHA, "
        "e-posta, şifre ve Oturum Aç işlemlerini tamamlayın."
    )
    try:
        await page.wait_for_url("**/company/**", timeout=timeout_ms)
    except Exception as exc:
        if page.is_closed():
            detail = "Chrome penceresi giriş tamamlanmadan kapatıldı"
        else:
            detail = await _visible_error(page)
        raise RuntimeError(
            "Simtes manuel girişi tamamlanmadı: " + detail
        ) from exc


async def _visible_error(page: Page) -> str:
    for selector in (
        ".alert:visible",
        ".invalid-feedback:visible",
        ".text-danger:visible",
        "[role='alert']:visible",
    ):
        locator = page.locator(selector)
        if await locator.count():
            text = " ".join((await locator.first.inner_text()).split())
            if text:
                return text[:300]
    return "portal giriş sayfasından yönlendirme yapmadı"


async def _read_draft_links(page: Page) -> list[dict]:
    await page.goto(DRAFTS_URL, wait_until="domcontentloaded", timeout=45_000)
    if "/login" in page.url:
        raise RuntimeError("Simtes oturumu doğrulanamadı")
    await page.locator("#kt_table_user tbody tr").first.wait_for(
        state="attached", timeout=30_000
    )

    drafts: list[dict] = []
    rows = page.locator("#kt_table_user tbody tr")
    for index in range(await rows.count()):
        row = rows.nth(index)
        cells = row.locator("td")
        if await cells.count() < 8:
            continue
        edit = row.locator('a[href*="/add-product-to-offer/"]').first
        href = await edit.get_attribute("href") if await edit.count() else ""
        if not href:
            continue
        drafts.append({
            "url": href if href.startswith("http") else f"{BASE_URL}{href}",
            "offer_date": _clean(await cells.nth(0).inner_text()),
            "offer_code": _clean(await cells.nth(1).inner_text()),
            "payment_type": _clean(await cells.nth(3).inner_text()),
        })
    return drafts


async def _read_offer_products(page: Page, draft: dict) -> list[dict]:
    # Angular tabloyu sonradan dolduruyor. Boş taslaklar da geçerli olduğundan
    # ilk satırı sınırlı süre bekleyip boş taslakları atlıyoruz.
    rows = page.locator("table tbody tr[ng-repeat*='latest']")
    try:
        await rows.first.wait_for(state="attached", timeout=10_000)
    except Exception:
        return []
    products: list[dict] = []
    for index in range(await rows.count()):
        cells = rows.nth(index).locator("td")
        if await cells.count() < 8:
            continue
        code = _clean(await cells.nth(0).inner_text())
        if not code:
            continue
        products.append({
            "product_code": code,
            "product_name": _clean(await cells.nth(1).inner_text()),
            "quantity": _clean(await cells.nth(2).inner_text()),
            "list_price": _clean(await cells.nth(3).inner_text()),
            "discount": _clean(await cells.nth(5).inner_text()),
            "net_price": _clean(await cells.nth(7).inner_text()),
            "currency": await _currency(cells.nth(3)),
            "stock": _clean(await cells.nth(6).inner_text()),
            "stock_status": _clean(await cells.nth(6).inner_text()),
            "delivery_date": _clean(await cells.nth(4).inner_text()),
            "offer_date": draft["offer_date"],
            "offer_code": draft["offer_code"],
            "payment_type": draft["payment_type"],
        })
    return products


async def _currency(cell) -> str:
    icon = " ".join(
        (await cell.locator("i").first.get_attribute("class") or "").split()
    ) if await cell.locator("i").count() else ""
    if "lira-sign" in icon:
        return "TRY"
    if "euro-sign" in icon:
        return "EUR"
    if "dollar-sign" in icon:
        return "USD"
    return ""


def _clean(value: str) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def _normalize_product_codes(lines) -> list[str]:
    """Boş/açıklama satırlarını atıp ürün kodlarını sıra koruyarak tekilleştir."""
    result: list[str] = []
    seen: set[str] = set()
    for line in lines:
        code = _clean(str(line))
        if not code or code.startswith("#"):
            continue
        key = code.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(code)
    return result


def _load_product_codes(path=DEFAULT_PRODUCT_CODES_PATH) -> list[str]:
    source = Path(path)
    if not source.exists():
        raise RuntimeError(f"Simtes ürün kodu dosyası bulunamadı: {source}")
    return _normalize_product_codes(source.read_text(encoding="utf-8-sig").splitlines())


def _quantity_number(row: dict) -> int:
    value = str(row.get("quantity", "")).strip().replace(",", ".")
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _summarize_offer_rows(rows: list[dict]) -> list[dict]:
    """100 adetlik sorgunun stokta/terminli bölümlerini tek satırda birleştir."""
    grouped: dict[tuple[str, str], list[dict]] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        key = (
            str(row.get("offer_code", "")),
            str(row.get("product_code", "")).strip().casefold(),
        )
        if not key[1]:
            continue
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(row)

    summarized: list[dict] = []
    for key in order:
        group = grouped[key]
        stocked = [
            row for row in group
            if "termin" not in str(row.get("stock_status", row.get("stock", ""))).casefold()
        ]
        termin = [row for row in group if row not in stocked]
        chosen = dict(stocked[0] if stocked else group[0])
        requested_qty = sum(_quantity_number(row) for row in group)
        stocked_qty = sum(_quantity_number(row) for row in stocked)

        if requested_qty >= 100:
            # Rapor yalnızca sorguda doğrulanan net adedi gösterir. Portal 100
            # adedin tamamına "Var" dediyse çıktı "100" olur; fazlasını
            # varsaymayız. Bölünmüş satırda ise stoktaki gerçek bölüm yazılır.
            chosen["stock"] = str(stocked_qty)
        else:
            chosen["stock"] = "Var" if stocked else "Terminli"
        chosen["stock_status"] = "Var" if stocked else "Terminli"
        summarized.append(chosen)
    return summarized


def _prefer_latest_product_rows(rows: list[dict]) -> list[dict]:
    """Aynı ürün için en yeni teklifi, aynı teklifte stoklu satırı seç."""
    selected: dict[str, dict] = {}
    order: list[str] = []
    for row in _summarize_offer_rows(rows):
        code = str(row.get("product_code", "")).strip()
        if not code:
            continue
        key = code.casefold()
        if key not in selected:
            selected[key] = row
            order.append(key)
    return [selected[key] for key in order]
