"""
Elektromarketim.com — fiyat scraper

DOM yapısı (gerçek HTML inceleme sonrası):
  Card  : div.productItem (col-xx classları ile)
  Brand : .vitrin-product-brand
  Title : .vitrin-product-title.detailLink-p.proLinkUrl
  Price : .cart-price  (KDV dahil)
  Soft  : .cart-soft-price  (KDV hariç — yedek)
  Old   : .cart-text-price (üstü çizili)
JSON-LD: Anasayfada Product şemaları var (42 adet) — kategoride yok.
Sayfalama: ?page={n}
Para birimi: TRY
"""
from panel2_fiyat.scrapers import BasePriceScraper, register_scraper
from panel2_fiyat.scrapers._generic import run_generic_scraper


BASE_URL = "https://www.elektromarketim.com"

DEFAULT_CATEGORIES = [
    "/c/led-ampuller",
    "/c/anahtar-priz",
    "/c/sigortalar",
    "/c/kontaktorler",
    "/c/role",
    "/c/kablo",
    "/c/aydinlatma",
]

SELECTORS = {
    "card":      "div.productItem",
    "brand":     ".vitrin-product-brand",
    "name":      ".vitrin-product-title",
    "price":     ".cart-price",
    "old_price": ".cart-text-price",
}


@register_scraper("elektromarketim")
class ElektromarketimScraper(BasePriceScraper):
    async def scrape(self) -> list[dict]:
        return await run_generic_scraper(
            base_url        = BASE_URL,
            category_paths  = DEFAULT_CATEGORIES,
            bestseller_path = None,
            pagination      = "?page={n}",
            max_pages       = 5,
            headless        = True,
            site_selectors  = SELECTORS,
            prefer_jsonld   = False,  # JSON-LD anasayfada, kategoride class kullan
        )
