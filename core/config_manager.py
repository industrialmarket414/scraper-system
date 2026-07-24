import json
from pathlib import Path
from fastapi import APIRouter, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.json"
BASE_DIR      = Path(__file__).parent.parent
templates     = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))
router        = APIRouter()

_DEFAULT = {
    "b2b_sites": [],
    "price_sites": [],
    "email": {
        "smtp_host":      "smtp.gmail.com",
        "smtp_port":      587,
        "sender_email":   "",
        "sender_password": "",
        "recipients":     [],
        "error_recipients": [],
    },
    "b2b_global_schedule": {
        "type":       "daily",
        "daily_time": "08:00",
    },
    "b2b_schedule": {
        "times":         ["08:00"],
        "enabled_sites": [],
        "combined_mail": True,
        "site_times":    {},
    },
    "price_schedule":    "0 */6 * * *",
    "price_max_records": 30,
    "claude_api_key":    "",
    "claude_model":      "claude-sonnet-4-5",
}


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        save_settings(_DEFAULT.copy())
        return _DEFAULT.copy()
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_settings(data: dict):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_b2b_site(site_id: str, updates: dict) -> bool:
    s = load_settings()
    for site in s.get("b2b_sites", []):
        if site["id"] == site_id:
            site.update(updates)
            save_settings(s)
            return True
    return False


# ── Ayarlar sayfası ───────────────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse(
        request, "settings.html", {"settings": load_settings(), "active": "settings"}
    )


@router.get("/api", response_class=JSONResponse)
async def get_settings_api():
    return load_settings()


@router.post("/api", response_class=JSONResponse)
async def update_settings_api(request: Request):
    from core.log_manager import add_log
    data = await request.json()
    save_settings(data)
    add_log("system", "settings", "Sistem", "info", "Ayarlar güncellendi")
    return {"status": "ok"}


# ── Test maili gönder ─────────────────────────────────────────────────────────
@router.post("/api/test-mail", response_class=JSONResponse)
async def test_mail_api():
    from core.mail import send_mail
    from core.log_manager import add_log
    cfg = load_settings()
    em  = cfg.get("email", {})

    if not em.get("sender_email") or not em.get("sender_password"):
        return JSONResponse(
            {"error": "SMTP bilgileri eksik. Ayarlar → E-posta bölümünden doldurun."},
            status_code=400
        )
    recipients = em.get("recipients", [])
    if not recipients:
        return JSONResponse(
            {"error": "Alıcı listesi boş. Mail Listeleri sayfasından ekleyin."},
            status_code=400
        )
    try:
        send_mail(
            recipients,
            "[ScrapeNet] Test Maili",
            "<p>Bu bir test mailidir.</p><p>ScrapeNet başarıyla mail gönderebiliyor ✓</p>"
        )
        add_log("system", "mail", "Test Mail", "info",
                f"Test maili gönderildi → {', '.join(recipients)}")
        return {"status": "ok", "sent_to": recipients}
    except Exception as e:
        add_log("system", "mail", "Test Mail", "error", "Test maili gönderilemedi", str(e))
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Mail listesi sayfası ──────────────────────────────────────────────────────
@router.get("/mail-lists", response_class=HTMLResponse)
async def mail_lists_page(request: Request):
    return templates.TemplateResponse(
        request, "mail_lists.html", {"settings": load_settings(), "active": "mail"}
    )


# ── Ürün eşleştirme sayfası ───────────────────────────────────────────────────
@router.get("/product-mapping", response_class=HTMLResponse)
async def product_mapping_page(request: Request):
    from core.product_mapping import get_summary, get_sample
    settings = load_settings()
    return templates.TemplateResponse(
        request, "product_mapping.html",
        {
            "settings": settings,
            "summary":  get_summary(),
            "sample":   get_sample(50),
            "sites":    settings.get("b2b_sites", []),
            "active":   "mapping",
        }
    )


@router.get("/api/product-mapping/summary", response_class=JSONResponse)
async def product_mapping_summary():
    from core.product_mapping import get_summary
    return get_summary()


@router.get("/api/product-mapping/template")
async def product_mapping_template():
    """Boş Excel template'ini indirir."""
    from core.product_mapping import generate_template
    settings = load_settings()
    data = generate_template(settings.get("b2b_sites", []))
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="urun_eslestirme_template.xlsx"'
        },
    )


@router.get("/api/product-mapping/download")
async def product_mapping_download():
    """DB'deki mevcut tüm eşleştirmeleri renkli Excel olarak indirir."""
    from core.product_mapping import generate_filled_export
    from datetime import datetime
    settings = load_settings()
    data = generate_filled_export(settings.get("b2b_sites", []))
    fname = f"urun_eslestirme_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/api/product-mapping/upload", response_class=JSONResponse)
async def product_mapping_upload(file: UploadFile = File(...)):
    from core.product_mapping import parse_excel, replace_all
    from core.log_manager import add_log

    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        return JSONResponse(
            {"error": "Sadece .xlsx / .xlsm dosyaları kabul edilir."},
            status_code=400,
        )

    try:
        content = await file.read()
        sites = load_settings().get("b2b_sites", [])
        rows, info = parse_excel(content, sites)
        n = replace_all(rows)
        add_log(
            "system", "mapping", "Ürün Eşleştirme", "info",
            f"Excel yüklendi → {n} eşleştirme kaydı oluşturuldu",
            f"Site bazında: {info['by_site']}; Atlanan başlıklar: {info['skipped_headers']}"
        )
        return {
            "status":           "ok",
            "saved":            n,
            "by_site":          info["by_site"],
            "skipped_headers":  info["skipped_headers"],
        }
    except Exception as e:
        add_log("system", "mapping", "Ürün Eşleştirme", "error",
                "Excel yüklenemedi", str(e))
        return JSONResponse({"error": str(e)}, status_code=500)


@router.delete("/api/product-mapping", response_class=JSONResponse)
async def product_mapping_clear():
    """Tüm eşleştirmeleri temizle."""
    from core.product_mapping import replace_all
    from core.log_manager import add_log
    replace_all([])
    add_log("system", "mapping", "Ürün Eşleştirme", "info",
            "Tüm eşleştirme kayıtları silindi")
    return {"status": "ok"}


# ── Sabit Stok sayfası ────────────────────────────────────────────────────────
@router.get("/fixed-stock", response_class=HTMLResponse)
async def fixed_stock_page(request: Request):
    from core.fixed_stock import get_summary, get_all_rows
    return templates.TemplateResponse(
        request, "fixed_stock.html",
        {
            "summary":  get_summary(),
            "rows":     get_all_rows(),
            "active":   "fixed_stock",
        }
    )


@router.get("/api/fixed-stock/template")
async def fixed_stock_template():
    from core.fixed_stock import generate_template
    return Response(
        content=generate_template(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="sabit_stok_template.xlsx"'},
    )


@router.get("/api/fixed-stock/download")
async def fixed_stock_download():
    from core.fixed_stock import generate_filled_export
    from datetime import datetime
    fname = f"sabit_stok_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return Response(
        content=generate_filled_export(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/api/fixed-stock/upload", response_class=JSONResponse)
async def fixed_stock_upload(file: UploadFile = File(...)):
    from core.fixed_stock import parse_excel, replace_all
    from core.log_manager import add_log

    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        return JSONResponse(
            {"error": "Sadece .xlsx / .xlsm dosyaları kabul edilir."},
            status_code=400,
        )

    try:
        content = await file.read()
        rows, info = parse_excel(content)
        n = replace_all(rows)
        add_log("system", "fixed_stock", "Sabit Stok", "info",
                f"Excel yüklendi → {n} sabit stok kaydı oluşturuldu",
                f"Atlanan: {info['skipped'][:20]}")
        return {
            "status":  "ok",
            "saved":   n,
            "skipped": info["skipped"],
        }
    except Exception as e:
        add_log("system", "fixed_stock", "Sabit Stok", "error",
                "Excel yüklenemedi", str(e))
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/fixed-stock/row", response_class=JSONResponse)
async def fixed_stock_upsert(request: Request):
    """Manuel ekleme/güncelleme — {em_code, qty}"""
    from core.fixed_stock import upsert_one
    data = await request.json()
    em = (data.get("em_code") or "").strip()
    qty = data.get("qty")
    if not em:
        return JSONResponse({"error": "em_code zorunlu"}, status_code=400)
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        return JSONResponse({"error": "qty geçerli bir sayı olmalı"}, status_code=400)
    if qty < 0:
        return JSONResponse({"error": "qty negatif olamaz"}, status_code=400)
    upsert_one(em, qty)
    return {"status": "ok"}


@router.delete("/api/fixed-stock/row/{em_code}", response_class=JSONResponse)
async def fixed_stock_delete_row(em_code: str):
    from core.fixed_stock import delete_one
    delete_one(em_code)
    return {"status": "ok"}


@router.delete("/api/fixed-stock", response_class=JSONResponse)
async def fixed_stock_clear():
    from core.fixed_stock import replace_all
    from core.log_manager import add_log
    replace_all([])
    add_log("system", "fixed_stock", "Sabit Stok", "info",
            "Tüm sabit stok kayıtları silindi")
    return {"status": "ok"}
