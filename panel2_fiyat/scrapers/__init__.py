"""
Fiyat scraper base sınıfı ve kayıt mekanizması.

Yeni bir fiyat scraper eklemek için:
  1. panel2_fiyat/scrapers/ altında yeni bir dosya oluştur (örn. site_trendyol.py)
  2. BasePriceScraper'dan türet ve @register_scraper("site_id") ile kaydet
  3. Bu dosyanın sonundaki import bloğuna dosyayı ekle

Örnek:
    from panel2_fiyat.scrapers import BasePriceScraper, register_scraper

    @register_scraper("trendyol")
    class TrendyolScraper(BasePriceScraper):
        async def scrape(self) -> list[dict]:
            return [
                {"product_code": "TY001", "product_name": "Ürün Adı",
                 "brand": "Marka", "price": 149.90, "old_price": 199.90,
                 "discount_percent": 25.0, "url": "https://..."}
            ]
"""
from abc import ABC, abstractmethod
from core.log_manager import add_log


class BasePriceScraper(ABC):
    def __init__(self, site_config: dict):
        self.site_id        = site_config["id"]
        self.site_name      = site_config["name"]
        self.url            = site_config["url"]
        self.username       = site_config.get("username", "")
        self.password       = site_config.get("password", "")
        self.is_our_catalog = site_config.get("is_our_catalog", False)
        self.brands         = site_config.get("brands", [])
        self.should_stop    = lambda: False

    @abstractmethod
    async def scrape(self) -> list[dict]:
        """
        Dönüş: list of dict, her dict şu anahtarları içerebilir:
          ZORUNLU:
            - product_name: str
            - price: float            (sepet/net fiyat — iskontolu)
          OPSİYONEL:
            - product_code: str
            - brand: str
            - category: str
            - old_price: float         (üstü çizili eski fiyat)
            - list_price: float        (ana birim liste fiyatı, iskonto öncesi)
            - discount_percent: float
            - currency: str            (varsayılan 'TRY')
            - is_bestseller: bool      (sitede "en çok satan/best seller" etiketi)
            - bestseller_label: str    ("Çok Satan", "Best Seller" vb. orijinal etiket)
            - em_code: str             (kendi katalogumuzdaki EM kodu — opsiyonel)
            - url: str
        """

    async def run(self) -> list[dict]:
        try:
            results = await self.scrape()
            add_log("panel2", self.site_id, self.site_name, "success",
                    f"{len(results)} ürün çekildi.")
            return results
        except Exception as e:
            add_log("panel2", self.site_id, self.site_name, "error",
                    "Scrape başarısız", str(e))
            raise


SCRAPER_REGISTRY: dict[str, type] = {}


def register_scraper(site_id: str):
    def decorator(cls):
        SCRAPER_REGISTRY[site_id] = cls
        return cls
    return decorator


def get_scraper(site_config: dict) -> BasePriceScraper:
    site_id = site_config["id"]
    cls = SCRAPER_REGISTRY.get(site_id)
    if cls is None:
        raise ValueError(f"'{site_id}' için fiyat scraper bulunamadı. "
                         f"Kayıtlı scraperlar: {list(SCRAPER_REGISTRY.keys())}")
    return cls(site_config)


# --- Scraper dosyalarını buraya import et ---
from panel2_fiyat.scrapers import site_elektromarketim  # noqa: E402, F401
from panel2_fiyat.scrapers import site_starakim          # noqa: E402, F401
from panel2_fiyat.scrapers import site_elektrix          # noqa: E402, F401
from panel2_fiyat.scrapers import site_elektrikmarket    # noqa: E402, F401
from panel2_fiyat.scrapers import site_tumpaelektrik     # noqa: E402, F401
from panel2_fiyat.scrapers import site_zeybekmarket      # noqa: E402, F401
from panel2_fiyat.scrapers import site_elektrikmalzemesi # noqa: E402, F401
from panel2_fiyat.scrapers import site_elektrikciden     # noqa: E402, F401
from panel2_fiyat.scrapers import site_ucfaz             # noqa: E402, F401
from panel2_fiyat.scrapers import site_aydinlatmadunyam  # noqa: E402, F401
from panel2_fiyat.scrapers import site_cimenelektrik      # noqa: E402, F401
from panel2_fiyat.scrapers import site_kozaydinlatma      # noqa: E402, F401
from panel2_fiyat.scrapers import site_krcelektromarket   # noqa: E402, F401
from panel2_fiyat.scrapers import site_buyukalpler        # noqa: E402, F401
from panel2_fiyat.scrapers import site_competitors       # noqa: E402, F401
