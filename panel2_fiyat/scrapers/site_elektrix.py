"""
Elektrix.com — fiyat scraper

DOM yapısı (gerçek HTML inceleme sonrası):
  Card  : div.productItem (col-xx classları ile, .zoom .ease)
  Price : .priceWrap (.priceWrapper içinde)
  Soft  : .cart-soft-price
  Title/Brand: Ürün başlıkları içinde (drop-down-title değil) — link text'inden alınır
Bestseller: /cok-satanlar URL'i mevcut → o sayfadan gelenler is_bestseller=True
Sayfalama: ?page={n}
Para birimi: TRY (TL ile gösterilir)
"""
from panel2_fiyat.scrapers import BasePriceScraper, register_scraper
from panel2_fiyat.scrapers._generic import run_generic_scraper


BASE_URL = "https://www.elektrix.com"

# Gerçek kategori URL'leri (anasayfa link analizi sonrası).
# NOT: Kategori sayfaları AJAX ile lazy yüklüyor, çoğu statik HTML'de 0 ürün
# veriyor. /cok-satanlar statik render ediyor (137 ürün) — ana kaynak budur.
DEFAULT_CATEGORIES = [
    "/led-ampul",
    "/armatur",
    "/kablo",
    "/roleler",
    "/kontaktorler",
    "/salt",
    "/parafudur",
    "/motor-koruma",
]

SELECTORS = {
    "card":      "div.productItem",
    "name":      "a[title]",          # ürün başlığı link title'da
    "price":     ".priceWrap",
    "old_price": ".cart-text-price",
}


@register_scraper("elektrix")
class ElektrixScraper(BasePriceScraper):
    async def scrape(self) -> list[dict]:
        return await run_generic_scraper(
            base_url        = BASE_URL,
            category_paths  = DEFAULT_CATEGORIES,
            bestseller_path = "/cok-satanlar",   # ÖNEMLİ — bestseller rozeti
            pagination      = "?page={n}",
            max_pages       = 5,
            headless        = True,
            site_selectors  = SELECTORS,
            prefer_jsonld   = False,
        )
