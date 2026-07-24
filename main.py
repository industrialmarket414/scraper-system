from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
from pathlib import Path
import uvicorn

from panel1_b2b.router import router as panel1_router
from panel2_fiyat.router import router as panel2_router
from core.config_manager import router as settings_router
from core.log_manager import router as logs_router
from core.database import init_db

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    (BASE_DIR / "logs").mkdir(exist_ok=True)
    (BASE_DIR / "panel1_b2b" / "outputs").mkdir(exist_ok=True)
    init_db()
    from panel1_b2b.scheduler import start_scheduler as start_b2b
    from panel2_fiyat.scheduler import start_scheduler as start_price
    start_b2b()
    start_price()
    yield
    from panel1_b2b.scheduler import stop_scheduler as stop_b2b
    from panel2_fiyat.scheduler import stop_scheduler as stop_price
    stop_b2b()
    stop_price()


app = FastAPI(title="ScrapeNet", version="1.0.0", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web" / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))

app.include_router(panel1_router, prefix="/panel1", tags=["Panel1"])
app.include_router(panel2_router, prefix="/panel2", tags=["Panel2"])
app.include_router(settings_router, prefix="/settings", tags=["Settings"])
app.include_router(logs_router, prefix="/logs", tags=["Logs"])


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    from core.config_manager import load_settings
    from core.database import get_conn

    settings = load_settings()
    b2b_sites   = settings.get("b2b_sites", [])
    price_sites = settings.get("price_sites", [])

    with get_conn() as conn:
        last_b2b = conn.execute(
            "SELECT last_run, last_status FROM b2b_jobs ORDER BY last_run DESC LIMIT 1"
        ).fetchone()
        last_price = conn.execute(
            "SELECT scraped_at FROM price_records ORDER BY scraped_at DESC LIMIT 1"
        ).fetchone()
        last_b2b_stock = conn.execute(
            "SELECT scraped_at FROM b2b_stock_records ORDER BY scraped_at DESC LIMIT 1"
        ).fetchone()

    stats = {
        "b2b_count":     len(b2b_sites),
        "price_count":   len(price_sites),
        "b2b_last_run":  (last_b2b["last_run"]   if last_b2b   else None),
        "b2b_last_ok":   (last_b2b["last_status"] == "success"  if last_b2b else None),
        "price_last_at": (last_price["scraped_at"] if last_price else
                          last_b2b_stock["scraped_at"] if last_b2b_stock else None),
    }

    return templates.TemplateResponse(
        request, "index.html", {"active": "home", "stats": stats}
    )


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
