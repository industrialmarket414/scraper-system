from core.database import get_conn
from core.config_manager import load_settings


def save_products(site_id: str, site_name: str, products: list[dict]):
    """
    Panel 2 ürünlerini DB'ye yazar. product_mapping varsa em_code enrichment'ı yapar.
    """
    settings    = load_settings()
    max_records = settings.get("price_max_records", 30)

    # EM kodu eşleştirmesi (Panel 1 ile aynı product_mapping tablosu)
    try:
        from core.product_mapping import load_mapping_for_site
        mapping = load_mapping_for_site(site_id)  # {site_code: em_code}
    except Exception:
        mapping = {}

    with get_conn() as conn:
        for p in products:
            code  = (p.get("product_code") or "").strip()
            em    = (p.get("em_code") or mapping.get(code) or "").strip()
            is_bs = 1 if p.get("is_bestseller") else 0
            conn.execute(
                """INSERT INTO price_records
                   (site_id, site_name, product_code, product_name, brand, category,
                    price, old_price, list_price, discount_percent,
                    currency, is_bestseller, bestseller_label, em_code, url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    site_id, site_name,
                    code,
                    p.get("product_name", ""),
                    p.get("brand"),
                    p.get("category"),
                    p.get("price"),
                    p.get("old_price"),
                    p.get("list_price"),
                    p.get("discount_percent"),
                    p.get("currency", "TRY"),
                    is_bs,
                    p.get("bestseller_label"),
                    em or None,
                    p.get("url"),
                ),
            )

        # Rolling window: site başına en fazla max_records kayıt
        conn.execute(
            """DELETE FROM price_records
               WHERE site_id = ? AND id NOT IN (
                   SELECT id FROM price_records
                   WHERE site_id = ?
                   ORDER BY scraped_at DESC
                   LIMIT ?
               )""",
            (site_id, site_id, max_records),
        )


def get_latest_products(site_id: str = None, limit: int = 100) -> list[dict]:
    with get_conn() as conn:
        if site_id:
            rows = conn.execute(
                "SELECT * FROM price_records WHERE site_id=? "
                "ORDER BY scraped_at DESC LIMIT ?",
                (site_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM price_records ORDER BY scraped_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
