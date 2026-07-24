import os
import re
from pathlib import Path
from playwright.async_api import async_playwright
from panel1_b2b.scrapers import BaseB2BScraper, register_scraper

# Playwright'ın tarayıcıları nerede arayacağını açıkça belirt
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(Path.home() / ".cache" / "ms-playwright")
)


def _parse_tr_price(text: str) -> float | None:
    """'1.234,56 TL' veya '22 TL' → 1234.56"""
    text = re.sub(r'[^\d,.]', '', text.strip())
    if ',' in text:
        integer_part = text.split(',')[0].replace('.', '')
        decimal_part = text.split(',')[1]
        text = f"{integer_part}.{decimal_part}"
    else:
        text = text.replace('.', '')
    try:
        return float(text) if text else None
    except ValueError:
        return None


def _parse_percent(text: str) -> float | None:
    m = re.search(r'[\d]+(?:[.,]\d+)?', text)
    return float(m.group().replace(',', '.')) if m else None


@register_scraper("noas")
class NoasScraper(BaseB2BScraper):
    COLUMNS = [
        ("product_code",  "Ürün Kodu",  18),
        ("stock",         "Stok",       22),
        ("adet",          "Koli Adeti", 14),
        ("price_text",    "Fiyat",      14),
        ("discount_text", "İskonto",    12),
    ]
    BASE_URL = "https://b2b.noas.com.tr"
    MAX_PAGES = 30

    async def scrape(self) -> list[dict]:
        products = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(viewport={"width": 1280, "height": 900})

            try:
                # ── Giriş — ana sekmede yap ──────────────────────────────
                login_page = await context.new_page()
                await login_page.goto(
                    f"{self.BASE_URL}/tr/giris",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                await login_page.fill("#userName", self.username)
                await login_page.fill("#password", self.password)
                await login_page.click(".btn-login")
                await login_page.wait_for_timeout(2000)
                await login_page.wait_for_function(
                    "!window.location.href.includes('/giris')",
                    timeout=15_000,
                )
                await login_page.close()  # giriş sekmesini kapat

                # ── Her sayfa için ayrı sekme aç → bitince kapat ─────────
                # Böylece DOM/bellek birikmez, her sayfa temiz başlar
                for page_num in range(1, self.MAX_PAGES + 1):
                    tab = await context.new_page()
                    try:
                        await tab.goto(
                            f"{self.BASE_URL}/tr/urunler?page={page_num}",
                            wait_until="domcontentloaded",
                            timeout=30_000,
                        )
                        await tab.wait_for_timeout(1000)  # 1500 → 1000ms

                        tbody = tab.locator("div.tbody-id")
                        if not await tbody.count():
                            break

                        links = await tbody.locator("a.row-id").all()
                        if not links:
                            break

                        for link in links:
                            item = {
                                "product_code":    "",
                                "stock":           "",
                                "adet":            "",
                                "price":           None,
                                "price_text":      "",
                                "discount_percent": None,
                                "discount_text":   "",
                            }

                            try:
                                item["product_code"] = (
                                    await link.locator("aside.code-id span")
                                              .inner_text(timeout=500)
                                ).strip()
                            except Exception:
                                pass

                            try:
                                item["stock"] = (
                                    await link.locator("span.bs-stocktext-exists")
                                              .inner_text(timeout=500)
                                ).strip()
                            except Exception:
                                pass

                            if not item["stock"]:
                                try:
                                    item["stock"] = (
                                        await link.locator("span.bs-stocktext-existsnot")
                                                  .inner_text(timeout=500)
                                    ).strip()
                                except Exception:
                                    pass

                            try:
                                item["adet"] = (
                                    await link.locator("article.fieldone-id span")
                                              .inner_text(timeout=500)
                                ).strip()
                            except Exception:
                                pass

                            try:
                                el = link.locator("main.normalprice-id").first
                                if await el.count():
                                    raw = (await el.inner_text(timeout=500)).strip()
                                    item["price_text"] = raw
                                    item["price"] = _parse_tr_price(raw)
                            except Exception:
                                pass

                            try:
                                el = link.locator("main.discount-id .bs-discount-outer").first
                                if await el.count():
                                    raw = (await el.inner_text(timeout=500)).strip()
                                    item["discount_text"] = raw
                                    item["discount_percent"] = _parse_percent(raw)
                            except Exception:
                                pass

                            if self.brands:
                                code = item["product_code"].upper()
                                if not any(b.upper() in code for b in self.brands):
                                    continue

                            products.append(item)

                    finally:
                        await tab.close()  # ← her sayfa bittikten sonra sekmeyi kapat

            finally:
                await browser.close()

        return products
