"""
Tumpaelektrik.com — fiyat scraper

DOM yapısı (gerçek HTML inceleme sonrası):
  Card        : div.productItem
  Title       : .productItem__title
  Price       : .productItem__price        (KDV dahil)
  Old price   : .productItem__priceOff     (eski / iskontolu öncesi)
  List price  : .productItem__priceRegular (liste fiyatı)
  KDV note    : .productItem__kdv
Anti-bot     : 403 dönmüştü → realistik User-Agent + Playwright (curl ile geçtik ama
               UA ile)
Bestseller   : Henüz tespit edilmedi
Sayfalama    : ?page={n}  (test edilecek)
Para birimi  : TRY
"""
from panel2_fiyat.scrapers import BasePriceScraper, register_scraper
from panel2_fiyat.scrapers._generic import run_generic_scraper


BASE_URL = "https://tumpaelektrik.com"

DEFAULT_CATEGORIES = [
    # PLACEHOLDER — gerçek kategori URL'leri test sonrası eklenecek
    "/aydinlatma",
    "/anahtar-priz",
    "/sigortalar",
    "/kablo",
]

SELECTORS = {
    "card":      "div.productItem",
    "name":      ".productItem__title",
    "price":     ".productItem__price",
    "old_price": ".productItem__priceOff",
}


@register_scraper("tumpaelektrik")
class TumpaScraper(BasePriceScraper):
    async def scrape(self) -> list[dict]:
        return await run_generic_scraper(
            base_url        = BASE_URL,
            category_paths  = DEFAULT_CATEGORIES,
            bestseller_path = None,
            pagination      = "?page={n}",
            max_pages       = 5,
            headless        = True,  # UA ile curl geçti — headless deneme
            site_selectors  = SELECTORS,
            prefer_jsonld   = False,
        )
