"""
Elektrikmarket.com.tr — fiyat scraper

DOM yapısı (gerçek HTML inceleme sonrası):
  Card  : div.showcase
  Brand : .showcase-brand
  Title : .showcase-title
  Price : .showcase-price-new       (KDV dahil yeni fiyat)
  Old   : .showcase-price-old       (üstü çizili eski fiyat)
  Label : .showcase-label-group-discount  (% iskonto rozeti)
  Bestseller etiketi yok (sadece YENİ/Tükendi/Kargo)
Sayfalama: ?tp={n}
Para birimi: TRY
"""
from panel2_fiyat.scrapers import BasePriceScraper, register_scraper
from panel2_fiyat.scrapers._generic import run_generic_scraper


BASE_URL = "https://www.elektrikmarket.com.tr"

DEFAULT_CATEGORIES = [
    "/kategori/dis-mekan-aydinlatma",
    "/kategori/ic-mekan-aydinlatma",
    "/kategori/anahtar-priz",
    "/kategori/vantilator",
    "/kategori/test-ve-olcu-aletleri",
    "/kategori/exit-acil-aydinlatma",
]

SELECTORS = {
    "card":      "div.showcase",
    "brand":     ".showcase-brand",
    "name":      ".showcase-title",
    "price":     ".showcase-price-new",
    "old_price": ".showcase-price-old",
}


@register_scraper("elektrikmarket")
class ElektrikmarketScraper(BasePriceScraper):
    async def scrape(self) -> list[dict]:
        return await run_generic_scraper(
            base_url        = BASE_URL,
            category_paths  = DEFAULT_CATEGORIES,
            bestseller_path = None,
            pagination      = "?tp={n}",
            max_pages       = 5,
            headless        = True,
            site_selectors  = SELECTORS,
            prefer_jsonld   = False,
        )
