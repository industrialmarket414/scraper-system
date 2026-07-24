"""
Avantaj / dezavantaj raporu.

Bizim katalogumuzdaki ürünleri rakiplerle karşılaştırıp avantajlı/geride
olduklarımızı sınıflandırır. Eşik %0 — her fark anlamlı.

Sınıflar:
  - en_ucuz         : bizim fiyat < tüm rakip fiyatlardan
  - ortalama_alti   : bizim < rakip ortalaması ama en ucuz değil
  - ortalama        : ±0.01 dahilinde
  - ortalama_ustu   : bizim > rakip ortalaması ama en pahalı değil
  - en_pahali       : bizim > tüm rakiplerden
  - tek_basina      : karşılaştırma için rakip yok
"""
from panel2_fiyat.comparison import compare_all


VERDICTS_GOOD = {"en_ucuz", "ortalama_alti"}
VERDICTS_BAD  = {"en_pahali", "ortalama_ustu"}


def full_report() -> dict:
    """
    Tüm katalogumuzu karşılaştırır, kategoriler halinde döndürür.
    """
    all_cmp = compare_all()

    buckets = {
        "en_ucuz":        [],
        "ortalama_alti":  [],
        "ortalama":       [],
        "ortalama_ustu":  [],
        "en_pahali":      [],
        "tek_basina":     [],
        "veri_yok":       [],
    }

    for c in all_cmp:
        pos = c.get("position")
        if not pos:
            buckets["veri_yok"].append(c)
            continue
        verdict = pos.get("verdict", "veri_yok")
        buckets.setdefault(verdict, []).append(c)

    # Liste fiyatı (iskonto öncesi) bazlı karşılaştırma için ek bucket
    list_price_diffs = []
    for c in all_cmp:
        our = c.get("our") or {}
        stats = c.get("stats") or {}
        our_lp  = our.get("list_price")
        avg_lp  = stats.get("avg_list_price")
        if our_lp is None or avg_lp is None:
            continue
        diff = round(our_lp - avg_lp, 2)
        diff_pct = round((our_lp - avg_lp) / avg_lp * 100, 2) if avg_lp else None
        list_price_diffs.append({
            "em_code":     c["em_code"],
            "our_list":    our_lp,
            "avg_list":    avg_lp,
            "diff":        diff,
            "diff_pct":    diff_pct,
        })

    # İskonto farkı (bizim iskonto vs rakip ortalaması)
    discount_diffs = []
    for c in all_cmp:
        our = c.get("our") or {}
        stats = c.get("stats") or {}
        our_d  = our.get("discount_percent")
        avg_d  = stats.get("avg_discount")
        if our_d is None or avg_d is None:
            continue
        diff = round(our_d - avg_d, 2)
        discount_diffs.append({
            "em_code":      c["em_code"],
            "our_discount": our_d,
            "avg_discount": avg_d,
            "diff":         diff,
        })

    summary = {
        "total":         len(all_cmp),
        "avantajli":     len(buckets["en_ucuz"]) + len(buckets["ortalama_alti"]),
        "rekabetci":     len(buckets["ortalama"]),
        "geride":        len(buckets["en_pahali"]) + len(buckets["ortalama_ustu"]),
        "veri_yok":      len(buckets["veri_yok"]) + len(buckets["tek_basina"]),
    }

    return {
        "summary":          summary,
        "buckets":          buckets,
        "list_price_diffs": sorted(list_price_diffs, key=lambda x: x["diff"]),
        "discount_diffs":   sorted(discount_diffs, key=lambda x: x["diff"], reverse=True),
    }


def top_advantages(limit: int = 20) -> list[dict]:
    """En çok avantajlı olduğumuz ürünler (en büyük negatif fark)."""
    all_cmp = compare_all()
    rows = []
    for c in all_cmp:
        pos = c.get("position")
        if not pos or pos.get("diff_pct_vs_avg") is None:
            continue
        if pos["diff_pct_vs_avg"] < 0:  # rakip ortalamasından ucuzuz
            rows.append({
                "em_code":         c["em_code"],
                "our_price":       pos["our_price"],
                "avg_competitor":  c["stats"]["avg_price"],
                "diff":            pos["diff_vs_avg"],
                "diff_pct":        pos["diff_pct_vs_avg"],
                "verdict":         pos["verdict"],
            })
    return sorted(rows, key=lambda x: x["diff_pct"])[:limit]


def top_disadvantages(limit: int = 20) -> list[dict]:
    """En çok geride olduğumuz ürünler (en büyük pozitif fark)."""
    all_cmp = compare_all()
    rows = []
    for c in all_cmp:
        pos = c.get("position")
        if not pos or pos.get("diff_pct_vs_avg") is None:
            continue
        if pos["diff_pct_vs_avg"] > 0:  # rakip ortalamasından pahalıyız
            rows.append({
                "em_code":         c["em_code"],
                "our_price":       pos["our_price"],
                "avg_competitor":  c["stats"]["avg_price"],
                "diff":            pos["diff_vs_avg"],
                "diff_pct":        pos["diff_pct_vs_avg"],
                "verdict":         pos["verdict"],
            })
    return sorted(rows, key=lambda x: x["diff_pct"], reverse=True)[:limit]
