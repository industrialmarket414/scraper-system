from core.database import get_conn


def get_bestsellers(site_id: str = None, limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        if site_id:
            rows = conn.execute(
                """SELECT product_name, brand, site_name,
                          COUNT(*) as gorulme,
                          ROUND(AVG(price),2) as ort_fiyat,
                          ROUND(MIN(price),2) as min_fiyat
                   FROM price_records WHERE site_id=?
                   GROUP BY product_name, site_id
                   ORDER BY gorulme DESC LIMIT ?""",
                (site_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT product_name, brand, site_name,
                          COUNT(*) as gorulme,
                          ROUND(AVG(price),2) as ort_fiyat,
                          ROUND(MIN(price),2) as min_fiyat
                   FROM price_records
                   GROUP BY product_name, site_id
                   ORDER BY gorulme DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_most_discounted(site_id: str = None, limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        if site_id:
            rows = conn.execute(
                """SELECT product_name, brand, site_name,
                          ROUND(price,2) as price,
                          ROUND(old_price,2) as old_price,
                          ROUND(discount_percent,1) as discount_percent, url
                   FROM price_records
                   WHERE site_id=? AND discount_percent IS NOT NULL
                   ORDER BY discount_percent DESC LIMIT ?""",
                (site_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT product_name, brand, site_name,
                          ROUND(price,2) as price,
                          ROUND(old_price,2) as old_price,
                          ROUND(discount_percent,1) as discount_percent, url
                   FROM price_records
                   WHERE discount_percent IS NOT NULL
                   ORDER BY discount_percent DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_cheapest(brand: str = None, limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        if brand:
            rows = conn.execute(
                """SELECT product_name, brand, site_name,
                          ROUND(MIN(price),2) as min_fiyat, url
                   FROM price_records
                   WHERE brand LIKE ? AND price IS NOT NULL
                   GROUP BY product_name
                   ORDER BY min_fiyat ASC LIMIT ?""",
                (f"%{brand}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT product_name, brand, site_name,
                          ROUND(MIN(price),2) as min_fiyat, url
                   FROM price_records
                   WHERE price IS NOT NULL
                   GROUP BY product_name
                   ORDER BY min_fiyat ASC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def search_combined(query: str, limit: int = 50) -> list[dict]:
    """
    Ürün kodu veya adı ile piyasa + B2B stoklarında birleşik arama.
    Her kayıtta 'source_type': 'market' | 'b2b' alanı bulunur.
    """
    with get_conn() as conn:
        market_rows = conn.execute(
            """SELECT 'market' as source_type,
                      site_name, product_code, product_name,
                      ROUND(price,2) as price, NULL as price_text,
                      ROUND(discount_percent,1) as discount_percent,
                      NULL as stock, NULL as adet, url, scraped_at
               FROM price_records
               WHERE product_code LIKE ? OR product_name LIKE ?
               ORDER BY scraped_at DESC LIMIT ?""",
            (f"%{query}%", f"%{query}%", limit),
        ).fetchall()

        b2b_rows = conn.execute(
            """SELECT 'b2b' as source_type,
                      site_name, product_code, '' as product_name,
                      ROUND(price,2) as price, price_text,
                      ROUND(discount_percent,1) as discount_percent,
                      stock, adet, NULL as url, scraped_at
               FROM b2b_stock_records
               WHERE product_code LIKE ?
               ORDER BY scraped_at DESC LIMIT ?""",
            (f"%{query}%", limit),
        ).fetchall()

        combined = [dict(r) for r in market_rows] + [dict(r) for r in b2b_rows]
        combined.sort(key=lambda x: x.get("scraped_at") or "", reverse=True)
        return combined[:limit]
