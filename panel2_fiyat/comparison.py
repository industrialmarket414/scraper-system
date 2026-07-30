"""
EM ürünü bazlı fiyat & iskonto karşılaştırma motoru.

Her EM kodu için:
  - Tüm sitelerden en son fiyatı çek
  - Bizim katalog (is_our_catalog=true site) fiyatımızı bul
  - net_fiyat farkı (price)
  - liste_fiyat farkı (list_price, iskonto öncesi ana birim)
  - iskonto farkı (discount_percent)
  - sıralama: en ucuz site, en yüksek iskontolu site

Eşik yok — her yüzde fark anlamlı.
"""
from core.database import get_conn
from core.config_manager import load_settings


PRICE_STEP = 0.01


def _our_site_ids() -> list[str]:
    """is_our_catalog=true olan site id'leri."""
    return [
        s["id"] for s in load_settings().get("price_sites", [])
        if s.get("is_our_catalog")
    ]


def _latest_per_site_for_em(em_code: str) -> list[dict]:
    """
    Her site için bu em_code'a ait EN SON kaydı döndürür.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT pr.*
               FROM price_records pr
               JOIN (
                   SELECT site_id, MAX(scraped_at) AS last_at
                   FROM price_records
                   WHERE em_code = ?
                   GROUP BY site_id
               ) latest
               ON pr.site_id = latest.site_id
              AND pr.scraped_at = latest.last_at
              AND pr.em_code = ?
               ORDER BY pr.price ASC""",
            (em_code, em_code),
        ).fetchall()
    return [dict(r) for r in rows]


def compare_em(em_code: str) -> dict:
    """
    Tek bir EM kodu için detaylı karşılaştırma.
    Döndürür: {
        em_code, our: {...} | None,
        competitors: [...],   # bizim hariç sıralı
        stats: {min_price, max_price, avg_price, ...},
        position: { ... }     # bizim pozisyonumuz
    }
    """
    rows = _latest_per_site_for_em(em_code)
    ours_ids = set(_our_site_ids())

    our_row = next((r for r in rows if r["site_id"] in ours_ids), None)
    competitors = [r for r in rows if r["site_id"] not in ours_ids]

    prices = [r["price"] for r in competitors if r.get("price") is not None]
    lists  = [r["list_price"] for r in competitors if r.get("list_price") is not None]
    discs  = [r["discount_percent"] for r in competitors if r.get("discount_percent") is not None]

    stats = {
        "competitor_count": len(competitors),
        "min_price":        min(prices) if prices else None,
        "max_price":        max(prices) if prices else None,
        "avg_price":        round(sum(prices) / len(prices), 2) if prices else None,
        "min_list_price":   min(lists) if lists else None,
        "avg_list_price":   round(sum(lists) / len(lists), 2) if lists else None,
        "min_discount":     min(discs) if discs else None,
        "max_discount":     max(discs) if discs else None,
        "avg_discount":     round(sum(discs) / len(discs), 1) if discs else None,
    }

    position = None
    if our_row and stats["avg_price"] is not None and our_row.get("price") is not None:
        our_price = our_row["price"]
        diff_avg  = round(our_price - stats["avg_price"], 2)
        diff_min  = round(our_price - stats["min_price"], 2)
        diff_pct_avg = round((our_price - stats["avg_price"]) / stats["avg_price"] * 100, 2) if stats["avg_price"] else None
        # Sıralama: kaçıncı sıradayız (1 = en ucuz)
        ranked = sorted(
            [r["price"] for r in rows if r.get("price") is not None]
        )
        rank = ranked.index(our_price) + 1 if our_price in ranked else None

        position = {
            "our_price":       our_price,
            "rank":            rank,
            "total":           len(ranked),
            "diff_vs_avg":     diff_avg,
            "diff_pct_vs_avg": diff_pct_avg,
            "diff_vs_min":     diff_min,
            "is_cheapest":     rank == 1 if rank else False,
            "is_most_expensive": rank == len(ranked) if rank else False,
            "verdict":         _classify(our_price, prices),
            "discount_offer":  _discount_offer(our_row, competitors),
        }

    return {
        "em_code":     em_code,
        "our":         our_row,
        "competitors": competitors,
        "stats":       stats,
        "position":    position,
    }


def _discount_offer(our_row: dict, competitors: list[dict]) -> dict | None:
    """En ucuz rakibi 0,01 TL geçmek için gereken iskontoyu hesaplar."""
    our_price = our_row.get("price")
    priced_competitors = [
        r for r in competitors
        if r.get("price") is not None and r["price"] >= 0
    ]
    if our_price is None or not priced_competitors:
        return None

    cheapest = min(priced_competitors, key=lambda r: r["price"])
    competitor_price = float(cheapest["price"])
    already_best = float(our_price) <= competitor_price
    target_price = round(
        float(our_price) if already_best else max(0.0, competitor_price - PRICE_STEP),
        2,
    )

    list_price = our_row.get("list_price") or our_row.get("old_price")
    required_total = None
    current_total = None
    if list_price is not None and float(list_price) > 0:
        list_price = float(list_price)
        required_total = round(
            max(0.0, min(100.0, (1 - target_price / list_price) * 100)), 2
        )
        current_total = round(
            max(0.0, min(100.0, (1 - float(our_price) / list_price) * 100)), 2
        )

    additional_rate = round(
        max(0.0, (float(our_price) - target_price) / float(our_price) * 100), 2
    ) if float(our_price) > 0 else 0.0

    return {
        "competitor_site_id": cheapest.get("site_id"),
        "competitor_site_name": cheapest.get("site_name"),
        "competitor_price": competitor_price,
        "target_price": target_price,
        "list_price": list_price,
        "current_total_discount_percent": current_total,
        "required_total_discount_percent": required_total,
        "additional_discount_percent": additional_rate,
        "additional_reduction_amount": round(
            max(0.0, float(our_price) - target_price), 2
        ),
        "already_best": already_best,
    }


def _classify(our_price: float, competitor_prices: list[float]) -> str:
    """
    Sınıflandırma (eşik %0 — her fark anlamlı):
      - 'en_ucuz' : Bizim fiyat < tüm rakip fiyatlardan
      - 'ortalama_alti' : Bizim < ortalama, ama en ucuz değil
      - 'ortalama' : Tam ortalamada (±0.01)
      - 'ortalama_ustu' : Bizim > ortalama, ama en pahalı değil
      - 'en_pahali' : Bizim > tüm rakiplerden
    """
    if not competitor_prices:
        return "tek_basina"
    if our_price < min(competitor_prices):
        return "en_ucuz"
    if our_price > max(competitor_prices):
        return "en_pahali"
    avg = sum(competitor_prices) / len(competitor_prices)
    if our_price < avg - 0.01:
        return "ortalama_alti"
    if our_price > avg + 0.01:
        return "ortalama_ustu"
    return "ortalama"


def compare_all() -> list[dict]:
    """
    Bizim katalogumuzda bulunan TÜM EM ürünleri için karşılaştırma listesi.
    """
    ours_ids = _our_site_ids()
    if not ours_ids:
        return []

    placeholders = ",".join("?" for _ in ours_ids)
    with get_conn() as conn:
        em_codes = [
            r["em_code"] for r in conn.execute(
                f"SELECT DISTINCT em_code FROM price_records "
                f"WHERE site_id IN ({placeholders}) AND em_code IS NOT NULL",
                ours_ids,
            ).fetchall()
        ]

    return [compare_em(em) for em in em_codes if em]
