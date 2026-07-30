"""İlave rakip mağazalar için ortak, yapılandırılabilir scraper tanımları."""
from panel2_fiyat.scrapers import BasePriceScraper, register_scraper
from panel2_fiyat.scrapers._generic import run_generic_scraper


IDEASOFT_SELECTORS = {
    "card": "div.showcase",
    "brand": ".showcase-brand",
    "name": ".showcase-title",
    "price": ".showcase-price-new",
    "old_price": ".showcase-price-old",
}

TSOFT_SELECTORS = {
    "card": "div.product-item",
    "brand": ".brand-title",
    "name": ".product-title",
    "price": ".current-price",
    "old_price": ".product-price-not-discounted",
}

TICIMAX_SELECTORS = {
    "card": "div.ItemOrj",
    "brand": ".productMarka",
    "name": ".productName",
    "price": ".discountPrice, .productPrice",
    "old_price": ".regularPrice",
    "code": ".productStokKodu",
}


class _ConfiguredCompetitor(BasePriceScraper):
    BASE_URL = ""
    CATEGORIES: list[str] = []
    SELECTORS: dict = {}
    PAGINATION = "?page={n}"
    MAX_PAGES = 5

    async def scrape(self) -> list[dict]:
        return await run_generic_scraper(
            base_url=self.BASE_URL,
            category_paths=self.CATEGORIES,
            pagination=self.PAGINATION,
            max_pages=self.MAX_PAGES,
            headless=True,
            site_selectors=self.SELECTORS,
            prefer_jsonld=False,
        )

