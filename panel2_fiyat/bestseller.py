"""
En çok satan (bestseller) analizi.

Sitelerin "Çok Satan / Best Seller / Popüler" gibi etiketlerle işaretlediği
ürünlerin tespiti ve karşılaştırması.
"""
from core.database import get_conn
from core.config_manager import load_settings


def _our_site_ids() -> list[str]:
    return [
        s["id"] for s in load_settings().get("price_sites", [])
        if s.get("is_our_catalog")
    ]


def by_site(limit_per_site: int = 50) -> dict:
    """
    Her site için bestseller ürünlerin listesi.
    Döndürür: {site_id: {site_name, count, items: [...]}}
    """
    with get_conn() as conn:
        sites = conn.execute(
            """SELECT site_id, site_name, COUNT(*) AS cnt
               FROM price_records
               WHERE is_bestseller = 1
               GROUP BY site_id"""
        ).fetchall()

        out: dict[str, dict] = {}
        for s in sites:
            items = conn.execute(
                """SELECT * FROM (
                       SELECT * FROM price_records
                       WHERE site_id = ? AND is_bestseller = 1
                       ORDER BY scraped_at DESC
                   )
                   GROUP BY COALESCE(product_code, product_name)
                   LIMIT ?""",
                (s["site_id"], limit_per_site),
            ).fetchall()
            out[s["site_id"]] = {
                "site_name": s["site_name"],
                "count":     s["cnt"],
                "items":     [dict(r) for r in items],
            }
    return out


def common_bestsellers(min_sites: int = 2, limit: int = 100) -> list[dict]:
    """
    Birden fazla sitede bestseller olan ürünler (piyasada gerçekten popüler).
    EM kodu üzerinden eşleştirir; em_code yoksa product_name normalize edilir.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT em_code, product_name, brand,
                      COUNT(DISTINCT site_id) AS site_count,
                      GROUP_CONCAT(DISTINCT site_name) AS sites,
                      ROUND(MIN(price), 2) AS min_price,
                      ROUND(MAX(price), 2) AS max_price,
                      ROUND(AVG(price), 2) AS avg_price
               FROM price_records
               WHERE is_bestseller = 1
                 AND em_code IS NOT NULL
               GROUP BY em_code
               HAVING site_count >= ?
               ORDER BY site_count DESC, em_code ASC
               LIMIT ?""",
            (min_sites, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def missing_in_our_catalog(min_sites: int = 2, limit: int = 100) -> list[dict]:
    """
    Birden fazla sitede bestseller olduğu halde bizim katalogumuzda OLMAYAN ürünler.
    Bunlar potansiyel olarak eklememiz gereken ürünler.
    """
    ours_ids = _our_site_ids()
    if not ours_ids:
        return []
    placeholders = ",".join("?" for _ in ours_ids)

    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT em_code, product_name, brand,
                       COUNT(DISTINCT site_id) AS site_count,
                       GROUP_CONCAT(DISTINCT site_name) AS sites,
                       ROUND(AVG(price), 2) AS avg_price
                FROM price_records
                WHERE is_bestseller = 1
                  AND em_code IS NOT NULL
                  AND em_code NOT IN (
                      SELECT DISTINCT em_code FROM price_records
                      WHERE site_id IN ({placeholders}) AND em_code IS NOT NULL
                  )
                GROUP BY em_code
                HAVING site_count >= ?
                ORDER BY site_count DESC
                LIMIT ?""",
            (*ours_ids, min_sites, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def brand_distribution(limit: int = 30) -> list[dict]:
    """
    Bestseller'larda hangi markaların ne kadar paya sahip olduğu.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT brand,
                      COUNT(*) AS bestseller_count,
                      COUNT(DISTINCT site_id) AS site_count,
                      COUNT(DISTINCT COALESCE(em_code, product_name)) AS unique_products
               FROM price_records
               WHERE is_bestseller = 1 AND brand IS NOT NULL AND brand != ''
               GROUP BY brand
               ORDER BY bestseller_count DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
