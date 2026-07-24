"""
B2B scraper base sınıfı ve kayıt mekanizması.

Yeni scraper eklemek:
  1. panel1_b2b/scrapers/ altında b2b_siteid.py oluştur
  2. BaseB2BScraper'dan türet + @register_scraper("site_id") ekle
  3. COLUMNS sınıf değişkenini tanımla (Excel sütunları)
  4. Bu dosyanın sonundaki import bloğuna ekle

Örnek:
    @register_scraper("abc")
    class AbcScraper(BaseB2BScraper):
        COLUMNS = [
            ("product_code", "Ürün Kodu", 18),
            ("stock",        "Stok",      20),
            ("price_text",   "Fiyat",     14),
        ]
        async def scrape(self) -> list[dict]:
            ...
"""
from abc import ABC, abstractmethod
from core.log_manager import add_log


class BaseB2BScraper(ABC):
    # Alt sınıflar kendi sütunlarını tanımlar; varsayılan genel format
    COLUMNS: list[tuple] = [
        ("product_code", "Ürün Kodu", 18),
        ("product_name", "Ürün Adı",  40),
        ("brand",        "Marka",     20),
        ("stock",        "Stok",      12),
        ("price",        "Fiyat",     14),
    ]

    def __init__(self, site_config: dict):
        self.site_id   = site_config["id"]
        self.site_name = site_config["name"]
        self.url       = site_config["url"]
        self.username  = site_config.get("username", "")
        self.password  = site_config.get("password", "")
        self.brands    = site_config.get("brands", [])

    @abstractmethod
    async def scrape(self) -> list[dict]:
        """Her ürün için dict döndür. Anahtarlar COLUMNS'taki key'lerle örtüşmeli."""

    async def run(self) -> list[dict]:
        try:
            results = await self.scrape()
            add_log("panel1", self.site_id, self.site_name, "success",
                    f"{len(results)} ürün çekildi.")
            return results
        except Exception as e:
            # notify=False: üst katman (scheduler) tek bir hata maili gönderir
            add_log("panel1", self.site_id, self.site_name, "error",
                    "Scrape başarısız", str(e), notify=False)
            raise


SCRAPER_REGISTRY: dict[str, type] = {}


def register_scraper(site_id: str):
    def decorator(cls):
        SCRAPER_REGISTRY[site_id] = cls
        return cls
    return decorator


def get_scraper(site_config: dict) -> BaseB2BScraper:
    site_id = site_config["id"]
    cls = SCRAPER_REGISTRY.get(site_id)
    if cls is None:
        raise ValueError(
            f"'{site_id}' için B2B scraper bulunamadı. "
            f"Kayıtlı: {list(SCRAPER_REGISTRY.keys())}"
        )
    return cls(site_config)


# ── Scraper dosyalarını buraya import et ──────────────────────────────────────
from panel1_b2b.scrapers import b2b_noas    # noqa: E402, F401
from panel1_b2b.scrapers import b2b_yildiz  # noqa: E402, F401
from panel1_b2b.scrapers import b2b_kirac    # noqa: E402, F401
from panel1_b2b.scrapers import b2b_kltshop  # noqa: E402, F401
from panel1_b2b.scrapers import b2b_konya    # noqa: E402, F401
from panel1_b2b.scrapers import b2b_mete     # noqa: E402, F401
from panel1_b2b.scrapers import b2b_anka     # noqa: E402, F401
from panel1_b2b.scrapers import b2b_santral  # noqa: E402, F401
from panel1_b2b.scrapers import b2b_vekmar   # noqa: E402, F401
from panel1_b2b.scrapers import b2b_meanwell # noqa: E402, F401
