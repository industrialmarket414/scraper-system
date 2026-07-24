"""
Panel 2 — Claude API ile yapay zeka analiz modülü.
28 kaynaktan (21 piyasa + 7 B2B) toplanan veriyi özetler,
Claude Sonnet API'ye gönderir ve Markdown rapor üretir.
"""
import asyncio
from typing import Optional
from core.database import get_conn
from core.config_manager import load_settings
from core.log_manager import add_log

# Analiz durumu (basit global state)
_state: dict = {"running": False, "last_id": None, "error": None}

SYSTEM_PROMPT = """\
Sen deneyimli bir ticari analiz ve satın alma stratejisti uzmansın.
Sana bir şirketin:
1. B2B tedarikçi stok verileri (ürün kodu, stok durumu, fiyat, iskonto)
2. Piyasa rakip site fiyat verileri (ürün adı, fiyat, iskonto yüzdesi)
bilgileri verilecek.

Bu verileri analiz ederek aşağıdaki başlıklar altında Türkçe rapor hazırla:

## 1. Yönetici Özeti
3-5 cümleyle genel durum ve en önemli bulgu.

## 2. Alım Önerileri
B2B stoklarında olan, piyasada talep gören ürünleri listele. Tablo formatında sun.
Her ürün için: Ürün Kodu / Ürün Adı | B2B Fiyatı | Piyasa Ortalama | Öneri

## 3. İndirim Yapılması Gereken Ürünler
Rakiplerden yüksek fiyatlı veya düşük iskontolu ürünleri belirle.
Rekabetçi kalabilmek için hangi ürünlerde indirim gerektiğini açıkla.

## 4. Stok Artırım Önerileri
Piyasada çok satılan, B2B'de düşük/yok stoklu ürünleri belirle.
Kaçırılan fırsat olarak değerlendir.

## 5. Satış Artırma Önerileri
Genel strateji, öne çıkarılabilecek ürün kategorileri, rakiplerin güçlü noktaları.

## 6. Dikkat Edilmesi Gereken Riskler
Stok riski, fiyat rekabeti, tedarik zinciri endişeleri.

Tablo kullan. Somut ürün adı/kodu ver. Veri yetersizse ilgili bölümü "Yeterli veri yok" yaz.
Çıktıyı Markdown formatında yaz.
"""


def _collect_data() -> dict:
    """SQLite'tan son 7 günlük piyasa + son 3 günlük B2B verisini çeker."""
    with get_conn() as conn:
        market = conn.execute(
            """SELECT site_name, product_name, brand,
                      ROUND(price,2) as price,
                      ROUND(old_price,2) as old_price,
                      ROUND(discount_percent,1) as discount_percent
               FROM price_records
               WHERE scraped_at >= datetime('now', '-7 days', 'localtime')
                 AND price IS NOT NULL
               ORDER BY discount_percent DESC NULLS LAST
               LIMIT 300"""
        ).fetchall()

        b2b = conn.execute(
            """SELECT site_name, product_code, stock,
                      price_text, ROUND(discount_percent,1) as discount_percent
               FROM b2b_stock_records
               WHERE scraped_at >= datetime('now', '-3 days', 'localtime')
               ORDER BY site_name, product_code
               LIMIT 500"""
        ).fetchall()

    return {
        "market": [dict(r) for r in market],
        "b2b":    [dict(r) for r in b2b],
    }


def _format_data(data: dict) -> str:
    lines = []

    # B2B stok özeti
    lines.append("=== B2B TEDARİKÇİ STOK VERİLERİ ===\n")
    if data["b2b"]:
        current_site = None
        for r in data["b2b"]:
            if r["site_name"] != current_site:
                current_site = r["site_name"]
                lines.append(f"\n[{current_site}]")
            disc = f" | İskonto: %{r['discount_percent']}" if r["discount_percent"] else ""
            lines.append(f"  {r['product_code']} | Stok: {r['stock']} | Fiyat: {r['price_text']}{disc}")
    else:
        lines.append("(B2B verisi bulunamadı)")

    # Piyasa fiyat özeti
    lines.append("\n\n=== PİYASA FİYAT VERİLERİ ===\n")
    if data["market"]:
        current_site = None
        for r in data["market"]:
            if r["site_name"] != current_site:
                current_site = r["site_name"]
                lines.append(f"\n[{current_site}]")
            disc = f" | İskonto: %{r['discount_percent']}" if r["discount_percent"] else ""
            lines.append(f"  {r['product_name']} | {r['price']} ₺{disc}")
    else:
        lines.append("(Piyasa verisi bulunamadı)")

    summary = (f"\n\nÖZET: {len(data['b2b'])} B2B ürün kaydı, "
               f"{len(data['market'])} piyasa fiyat kaydı analiz edildi.")
    lines.append(summary)
    return "\n".join(lines)


async def run_analysis() -> dict:
    """Analizi arka planda çalıştırır. Sonucu DB'ye kaydeder."""
    global _state
    if _state["running"]:
        raise RuntimeError("Analiz zaten çalışıyor. Lütfen bekleyin.")

    settings = load_settings()
    api_key  = settings.get("claude_api_key", "").strip()
    model    = settings.get("claude_model", "claude-sonnet-4-5")

    if not api_key:
        raise ValueError(
            "Claude API anahtarı ayarlanmamış. "
            "Ayarlar → AI Ayarları bölümünden ekleyin."
        )

    _state = {"running": True, "last_id": None, "error": None}

    try:
        data      = _collect_data()
        formatted = _format_data(data)

        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            raise ImportError(
                "anthropic paketi kurulu değil. "
                "Terminalde: pip install anthropic"
            )

        client   = AsyncAnthropic(api_key=api_key)
        response = await client.messages.create(
            model=model,
            max_tokens=4096,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": formatted}],
        )

        report_text = response.content[0].text
        tokens      = response.usage.input_tokens + response.usage.output_tokens
        input_summary = (f"{len(data['b2b'])} B2B ürün, "
                         f"{len(data['market'])} piyasa kaydı")

        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO ai_reports
                   (report_type, input_summary, report_text, token_count)
                   VALUES (?,?,?,?)""",
                ("full", input_summary, report_text, tokens)
            )
            report_id = cur.lastrowid

        add_log("panel2", "ai", "AI Analiz", "success",
                f"Rapor oluşturuldu ({tokens} token)")

        _state = {"running": False, "last_id": report_id, "error": None}
        return {"report_id": report_id, "tokens": tokens}

    except Exception as e:
        add_log("panel2", "ai", "AI Analiz", "error", "Analiz başarısız", str(e))
        _state = {"running": False, "last_id": _state.get("last_id"), "error": str(e)}
        raise


def get_analysis_state() -> dict:
    return _state.copy()


def get_reports(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, created_at, report_type, input_summary, token_count
               FROM ai_reports ORDER BY created_at DESC LIMIT ?""",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_report_by_id(report_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ai_reports WHERE id=?", (report_id,)
        ).fetchone()
    if not row:
        return None
    report = dict(row)
    try:
        import markdown as md
        report["report_html"] = md.markdown(
            report["report_text"],
            extensions=["tables", "nl2br"]
        )
    except ImportError:
        # markdown paketi yoksa ham metni <pre> içinde göster
        report["report_html"] = f"<pre>{report['report_text']}</pre>"
    return report
