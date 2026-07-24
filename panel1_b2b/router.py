import asyncio
from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from core.config_manager import load_settings, update_b2b_site
from core.database import get_conn
from core.log_manager import add_log
from panel1_b2b.scheduler import run_b2b_site, reload_scheduler
from panel1_b2b.task_manager import is_running, cancel, running_ids

BASE_DIR  = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))
LOGOS_DIR = BASE_DIR / "web" / "static" / "logos"
LOGOS_DIR.mkdir(parents=True, exist_ok=True)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def panel1_page(request: Request):
    settings = load_settings()
    sites    = settings.get("b2b_sites", [])
    with get_conn() as conn:
        jobs = {r["site_id"]: dict(r)
                for r in conn.execute("SELECT * FROM b2b_jobs").fetchall()}
    for s in sites:
        j = jobs.get(s["id"], {})
        s["last_run"]    = j.get("last_run")    or "—"
        s["last_status"] = j.get("last_status") or "-"
        s["output_file"] = j.get("output_file") or ""
    return templates.TemplateResponse(
        request, "panel1.html", {"sites": sites, "active": "panel1"}
    )


# ── Durum (running dahil) ─────────────────────────────────────────────────────
@router.get("/api/status", response_class=JSONResponse)
async def get_status():
    settings = load_settings()
    active   = running_ids()
    with get_conn() as conn:
        jobs = {r["site_id"]: dict(r)
                for r in conn.execute("SELECT * FROM b2b_jobs").fetchall()}
    return [
        {
            "id":          s["id"],
            "name":        s["name"],
            "logo_url":    s.get("logo_url"),
            "enabled":     s.get("enabled", True),
            "last_run":    jobs.get(s["id"], {}).get("last_run")    or "—",
            "last_status": jobs.get(s["id"], {}).get("last_status") or "-",
            "has_output":  bool(jobs.get(s["id"], {}).get("output_file")),
            "running":     s["id"] in active,
        }
        for s in settings.get("b2b_sites", [])
    ]


# ── Aktif çekimler listesi ────────────────────────────────────────────────────
@router.get("/api/running", response_class=JSONResponse)
async def get_running():
    return {"running": running_ids()}


# ── Manuel çalıştır ───────────────────────────────────────────────────────────
@router.post("/api/run/{site_id}", response_class=JSONResponse)
async def manual_run(site_id: str):
    settings = load_settings()
    site = next((s for s in settings.get("b2b_sites", []) if s["id"] == site_id), None)
    if not site:
        return JSONResponse({"error": "Site bulunamadı"}, status_code=404)

    if is_running(site_id):
        return JSONResponse(
            {"error": f"{site['name']} şu an çekiyor. Durdurmak için ⏹ butonuna basın."},
            status_code=409,
        )

    add_log("panel1", site_id, site["name"], "info",
            f"{site['name']} — Manuel veri çekimi başlatıldı")

    # asyncio.create_task → task_manager'a register edilecek
    asyncio.create_task(run_b2b_site(site, "manual"))
    return {"status": "started", "site_id": site_id}


# ── Çekimi durdur ─────────────────────────────────────────────────────────────
@router.delete("/api/run/{site_id}", response_class=JSONResponse)
async def cancel_run(site_id: str):
    settings = load_settings()
    site = next((s for s in settings.get("b2b_sites", []) if s["id"] == site_id), None)
    site_name = site["name"] if site else site_id

    if cancel(site_id):
        add_log("panel1", site_id, site_name, "info",
                f"{site_name} — Çekim durduruldu", notify=False)
        return {"status": "cancelled"}
    return JSONResponse({"error": "Bu site için aktif çekim yok"}, status_code=404)


# ── Geçmiş ────────────────────────────────────────────────────────────────────
@router.get("/api/history/{site_id}", response_class=JSONResponse)
async def get_history(site_id: str):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM b2b_scrape_history
               WHERE site_id=? ORDER BY scraped_at DESC LIMIT 30""",
            (site_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Kimlik bilgilerini güncelle ───────────────────────────────────────────────
@router.post("/api/site/{site_id}/settings", response_class=JSONResponse)
async def update_site_settings(site_id: str, request: Request):
    data = await request.json()
    settings = load_settings()
    site = next((s for s in settings.get("b2b_sites", []) if s["id"] == site_id), None)
    if not site:
        return JSONResponse({"error": "Site bulunamadı"}, status_code=404)
    site_name = site["name"]
    changes = []
    if "username" in data and data["username"]:
        changes.append("kullanıcı adı")
    if "password" in data and data["password"]:
        changes.append("şifre")
    if not update_b2b_site(site_id, data):
        return JSONResponse({"error": "Güncelleme başarısız"}, status_code=500)
    if changes:
        add_log("panel1", site_id, site_name, "info",
                f"{site_name} — Kimlik bilgileri güncellendi",
                f"Değiştirilen: {', '.join(changes)}")
    reload_scheduler()
    return {"status": "ok"}


# ── Mail gönder ───────────────────────────────────────────────────────────────
@router.post("/api/mail/{site_id}", response_class=JSONResponse)
async def send_site_mail(site_id: str, request: Request, background_tasks: BackgroundTasks):
    from core.mail import send_mail
    data       = await request.json()
    recipients = data.get("recipients", [])
    if not recipients:
        return JSONResponse({"error": "Gönderilecek alıcı seçilmedi."}, status_code=400)
    settings  = load_settings()
    site      = next((s for s in settings.get("b2b_sites", []) if s["id"] == site_id), None)
    site_name = site["name"] if site else site_id
    with get_conn() as conn:
        job = conn.execute("SELECT * FROM b2b_jobs WHERE site_id=?", (site_id,)).fetchone()
    output_file = job["output_file"] if job else None
    if not output_file or not Path(output_file).exists():
        return JSONResponse(
            {"error": "Gönderilecek Excel dosyası yok. Önce veri çekin."},
            status_code=400
        )
    def _send():
        try:
            send_mail(recipients, f"[ScrapeNet] {site_name} Stok Raporu",
                      f"<p><b>{site_name}</b> stok raporu ektedir.</p>",
                      attachments=[Path(output_file)])
            add_log("panel1", site_id, site_name, "info",
                    f"{site_name} — Mail gönderildi",
                    f"Alıcılar: {', '.join(recipients)}")
        except Exception as e:
            add_log("panel1", site_id, site_name, "error",
                    f"{site_name} — Mail gönderilemedi", str(e))
    background_tasks.add_task(_send)
    return {"status": "ok", "recipients": recipients}


# ── Logo yükle ────────────────────────────────────────────────────────────────
import shutil
from fastapi import File, UploadFile

@router.post("/api/site/{site_id}/logo")
async def upload_logo(site_id: str, logo: UploadFile = File(...)):
    settings = load_settings()
    site = next((s for s in settings.get("b2b_sites", []) if s["id"] == site_id), None)
    if not site:
        return JSONResponse({"error": "Site bulunamadı"}, status_code=404)
    for old in LOGOS_DIR.glob(f"{site_id}.*"):
        old.unlink(missing_ok=True)
    ext  = logo.filename.rsplit(".", 1)[-1].lower() if "." in logo.filename else "png"
    path = LOGOS_DIR / f"{site_id}.{ext}"
    with open(path, "wb") as f:
        shutil.copyfileobj(logo.file, f)
    logo_url = f"/static/logos/{site_id}.{ext}"
    update_b2b_site(site_id, {"logo_url": logo_url})
    add_log("panel1", site_id, site["name"], "info", f"{site['name']} — Logo güncellendi")
    return {"status": "ok", "logo_url": logo_url}


# ── Excel indir ───────────────────────────────────────────────────────────────
@router.get("/api/download/{site_id}")
async def download_output(site_id: str):
    with get_conn() as conn:
        job = conn.execute("SELECT * FROM b2b_jobs WHERE site_id=?", (site_id,)).fetchone()
    if not job or not job["output_file"]:
        return JSONResponse({"error": "Dosya bulunamadı"}, status_code=404)
    p = Path(job["output_file"])
    if not p.exists():
        return JSONResponse({"error": "Dosya mevcut değil"}, status_code=404)
    return FileResponse(str(p), filename=p.name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@router.post("/api/reload-scheduler", response_class=JSONResponse)
async def reload_sched():
    reload_scheduler()
    return {"status": "ok"}
