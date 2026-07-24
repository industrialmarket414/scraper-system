"""
Starakim.com — fiyat scraper

DOM yapısı (gerçek HTML inceleme sonrası):
  Card  : div.product-item (col-xx classları ile)
  Brand : .brand-title.fw-semibold
  Title : .product-title
  Price : .current-price.fw-semibold
  Old   : .product-price-not-discounted (üstü çizili)
Sayfalama: ?pg={n}
Para birimi: TRY
"""
from panel2_fiyat.scrapers import BasePriceScraper, register_scraper
from panel2_fiyat.scrapers._generic import run_generic_scraper


BASE_URL = "https://www.starakim.com"

DEFAULT_CATEGORIES = [
    "/led-ampuller",
    "/anahtar-priz",
    "/sigortalar",
    "/kablo",
    "/aydinlatma-armaturleri",
]

SELECTORS = {
    "card":      "div.product-item",
    "brand":     ".brand-title",
    "name":      ".product-title",
    "price":     ".current-price",
    "old_price": ".product-price-not-discounted",
}


@register_scraper("starakim")
class StarakimScraper(BasePriceScraper):
    async def scrape(self) -> list[dict]:
        return await run_generic_scraper(
            base_url        = BASE_URL,
            category_paths  = DEFAULT_CATEGORIES,
            bestseller_path = None,
            pagination      = "?pg={n}",
            max_pages       = 5,
            headless        = True,
            site_selectors  = SELECTORS,
            prefer_jsonld   = False,
        )
