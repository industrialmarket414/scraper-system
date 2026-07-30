import shutil
from fastapi import APIRouter, Request, BackgroundTasks, File, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from core.config_manager import load_settings, save_settings
from panel2_fiyat.analyzer import (
    get_bestsellers, get_most_discounted, get_cheapest, search_combined
)
from panel2_fiyat.database import get_latest_products
from panel2_fiyat.scheduler import run_all_price_sites, run_single_price_site, reload_scheduler
from panel2_fiyat.ai_analyzer import (
    run_analysis, get_analysis_state, get_reports, get_report_by_id
)
from panel2_fiyat import comparison as cmp_mod
from panel2_fiyat import bestseller as bs_mod
from panel2_fiyat import advantage as adv_mod

BASE_DIR  = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))
LOGOS_DIR = BASE_DIR / "web" / "static" / "logos"
LOGOS_DIR.mkdir(parents=True, exist_ok=True)
router    = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def panel2_page(request: Request):
    settings = load_settings()
    return templates.TemplateResponse(
        request, "panel2.html", {
            "sites":     settings.get("price_sites", []),
            "b2b_sites": settings.get("b2b_sites", []),
            "active":    "panel2",
        }
    )


# ── Scrape ────────────────────────────────────────────────────────────────────
@router.post("/api/run/all", response_class=JSONResponse)
async def run_all(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_all_price_sites)
    return {"status": "started"}


@router.post("/api/run/{site_id}", response_class=JSONResponse)
async def run_site(site_id: str, background_tasks: BackgroundTasks):
    settings = load_settings()
    site = next((s for s in settings.get("price_sites", []) if s["id"] == site_id), None)
    if not site:
        return JSONResponse({"error": "Site bulunamadı"}, status_code=404)
    background_tasks.add_task(run_single_price_site, site)
    return {"status": "started", "site_id": site_id}


@router.get("/api/download/{site_id}")
async def download_excel(site_id: str):
    from panel2_fiyat.excel_exporter import latest_output
    settings = load_settings()
    site = next(
        (s for s in settings.get("price_sites", []) if s["id"] == site_id),
        None,
    )
    if not site:
        return JSONResponse({"error": "Site bulunamadı"}, status_code=404)
    output = latest_output(site_id)
    if not output:
        return JSONResponse(
            {"error": "Henüz Excel oluşmadı. Önce siteyi tarayın."},
            status_code=404,
        )
    return FileResponse(
        output,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        filename=output.name,
    )


# ── Analiz sorguları ──────────────────────────────────────────────────────────
@router.get("/api/products",            response_class=JSONResponse)
async def products(site_id: str = None, limit: int = 100):
    return get_latest_products(site_id=site_id, limit=limit)

@router.get("/api/analyze/bestsellers", response_class=JSONResponse)
async def bestsellers(site_id: str = None, limit: int = 20):
    return get_bestsellers(site_id=site_id, limit=limit)

@router.get("/api/analyze/discounts",   response_class=JSONResponse)
async def discounts(site_id: str = None, limit: int = 20):
    return get_most_discounted(site_id=site_id, limit=limit)

@router.get("/api/analyze/cheapest",    response_class=JSONResponse)
async def cheapest(brand: str = None, limit: int = 20):
    return get_cheapest(brand=brand, limit=limit)

@router.get("/api/search",              response_class=JSONResponse)
async def search(q: str, limit: int = 50):
    return search_combined(query=q, limit=limit)


# ── AI Analiz ─────────────────────────────────────────────────────────────────
@router.post("/api/ai/analyze", response_class=JSONResponse)
async def ai_analyze(background_tasks: BackgroundTasks):
    state = get_analysis_state()
    if state["running"]:
        return JSONResponse({"error": "Analiz zaten çalışıyor"}, status_code=409)
    background_tasks.add_task(run_analysis)
    return {"status": "started"}

@router.get("/api/ai/status", response_class=JSONResponse)
async def ai_status():
    return get_analysis_state()

@router.get("/api/ai/reports", response_class=JSONResponse)
async def ai_reports(limit: int = 20):
    return get_reports(limit=limit)

@router.get("/api/ai/reports/{report_id}", response_class=JSONResponse)
async def ai_report(report_id: int):
    r = get_report_by_id(report_id)
    if not r:
        return JSONResponse({"error": "Rapor bulunamadı"}, status_code=404)
    return r

@router.post("/api/reload-scheduler", response_class=JSONResponse)
async def reload_sched():
    reload_scheduler()
    return {"status": "ok"}


# ── Karşılaştırma & Avantaj Analizi ───────────────────────────────────────────
@router.get("/api/comparison/em/{em_code}", response_class=JSONResponse)
async def comparison_em(em_code: str):
    """Tek bir EM ürünü için detaylı karşılaştırma."""
    return cmp_mod.compare_em(em_code)


@router.get("/api/comparison/all", response_class=JSONResponse)
async def comparison_all():
    """Bizim katalogumuzdaki tüm EM ürünleri karşılaştırması."""
    return cmp_mod.compare_all()


@router.get("/api/advantage/report", response_class=JSONResponse)
async def advantage_report():
    """Avantajlı/geride sınıflandırması — özet + bucket'lar."""
    return adv_mod.full_report()


@router.get("/api/advantage/top", response_class=JSONResponse)
async def advantage_top(limit: int = 20):
    """En çok avantajlı olduğumuz ürünler (en büyük negatif fark)."""
    return adv_mod.top_advantages(limit=limit)


@router.get("/api/advantage/bottom", response_class=JSONResponse)
async def advantage_bottom(limit: int = 20):
    """En çok geride olduğumuz ürünler (en büyük pozitif fark)."""
    return adv_mod.top_disadvantages(limit=limit)


# ── Bestseller Analizi ────────────────────────────────────────────────────────
@router.get("/api/bestseller/by-site", response_class=JSONResponse)
async def bestseller_by_site(limit_per_site: int = 50):
    return bs_mod.by_site(limit_per_site=limit_per_site)


@router.get("/api/bestseller/common", response_class=JSONResponse)
async def bestseller_common(min_sites: int = 2, limit: int = 100):
    """Birden fazla sitede bestseller olan ürünler."""
    return bs_mod.common_bestsellers(min_sites=min_sites, limit=limit)


@router.get("/api/bestseller/missing", response_class=JSONResponse)
async def bestseller_missing(min_sites: int = 2, limit: int = 100):
    """Bestseller olduğu halde bizde olmayan ürünler."""
    return bs_mod.missing_in_our_catalog(min_sites=min_sites, limit=limit)


@router.get("/api/bestseller/brands", response_class=JSONResponse)
async def bestseller_brands(limit: int = 30):
    """Bestseller'larda marka dağılımı."""
    return bs_mod.brand_distribution(limit=limit)


# ── Logo upload (Panel 1 ile aynı pattern) ───────────────────────────────────
def _update_price_site(site_id: str, updates: dict) -> bool:
    s = load_settings()
    for site in s.get("price_sites", []):
        if site["id"] == site_id:
            site.update(updates)
            save_settings(s)
            return True
    return False


@router.post("/api/site/{site_id}/logo")
async def upload_logo(site_id: str, logo: UploadFile = File(...)):
    settings = load_settings()
    site = next((s for s in settings.get("price_sites", []) if s["id"] == site_id), None)
    if not site:
        return JSONResponse({"error": "Site bulunamadı"}, status_code=404)

    # Eski logoyu temizle
    for old in LOGOS_DIR.glob(f"price_{site_id}.*"):
        old.unlink(missing_ok=True)

    ext  = logo.filename.rsplit(".", 1)[-1].lower() if "." in logo.filename else "png"
    path = LOGOS_DIR / f"price_{site_id}.{ext}"
    with open(path, "wb") as f:
        shutil.copyfileobj(logo.file, f)

    logo_url = f"/static/logos/price_{site_id}.{ext}"
    _update_price_site(site_id, {"logo_url": logo_url})

    from core.log_manager import add_log
    add_log("panel2", site_id, site["name"], "info", f"{site['name']} — Logo güncellendi")
    return {"status": "ok", "logo_url": logo_url}


@router.delete("/api/site/{site_id}/logo", response_class=JSONResponse)
async def delete_logo(site_id: str):
    settings = load_settings()
    site = next((s for s in settings.get("price_sites", []) if s["id"] == site_id), None)
    if not site:
        return JSONResponse({"error": "Site bulunamadı"}, status_code=404)
    for old in LOGOS_DIR.glob(f"price_{site_id}.*"):
        old.unlink(missing_ok=True)
    _update_price_site(site_id, {"logo_url": None})
    return {"status": "ok"}


@router.post("/api/site/{site_id}/our-catalog", response_class=JSONResponse)
async def toggle_our_catalog(site_id: str, request: Request):
    """Bir fiyat sitesini 'Bizim Katalog' olarak işaretle/kaldır."""
    data = await request.json()
    flag = bool(data.get("is_our_catalog"))
    if not _update_price_site(site_id, {"is_our_catalog": flag}):
        return JSONResponse({"error": "Site bulunamadı"}, status_code=404)
    return {"status": "ok", "is_our_catalog": flag}
