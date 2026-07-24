from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from core.database import get_conn

BASE_DIR  = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))
router    = APIRouter()

# Hata bildirimi gönderilirken tekrar loglama döngüsünü engelle
_notifying = False


def add_log(panel: str, site_id: str, site_name: str,
            status: str, message: str, details: str = "",
            notify: bool = True):
    """
    Her türlü aktiviteyi loglar.
    status: 'success' | 'error' | 'info'
    notify=True → status='error' olduğunda hata bildirimi maili gönderilir.
    notify=False → sadece loglar, mail atmaz (tekrar bildirimi önlemek için).
    """
    global _notifying
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO logs (panel, site_id, site_name, status, message, details)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (panel, site_id, site_name, status, message, details)
        )

    if status == "error" and notify and not _notifying:
        _notifying = True
        try:
            from core.mail import send_error_mail
            panel_label = "B2B Stok" if panel == "panel1" else "Fiyat Analiz"
            send_error_mail(
                f"{site_name} — {message}",
                f"""<p><b>Panel:</b> {panel_label}</p>
                    <p><b>Site:</b> {site_name}</p>
                    <p><b>Hata:</b> {message}</p>
                    {f'<p><b>Detay:</b><br><code>{details}</code></p>' if details else ''}"""
            )
        except Exception:
            pass
        finally:
            _notifying = False


def get_logs(limit: int = 500, panel: str = None,
             status: str = None, days: int = 15) -> list:
    with get_conn() as conn:
        conditions, params = [], []
        if days and days > 0:
            conditions.append(
                f"timestamp >= datetime('now', '-{int(days)} days', 'localtime')"
            )
        if panel:
            conditions.append("panel = ?")
            params.append(panel)
        if status:
            conditions.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM logs {where} ORDER BY timestamp DESC LIMIT ?", params
        ).fetchall()
        return [dict(r) for r in rows]


@router.get("/", response_class=HTMLResponse)
async def logs_page(request: Request):
    logs = get_logs(limit=500, days=15)
    return templates.TemplateResponse(
        request, "logs.html", {"logs": logs, "active": "logs"}
    )


@router.get("/api", response_class=JSONResponse)
async def get_logs_api(limit: int = 500, panel: str = None,
                        status: str = None, days: int = 15):
    return get_logs(limit=limit, panel=panel, status=status, days=days)


@router.delete("/api", response_class=JSONResponse)
async def clear_logs_api():
    with get_conn() as conn:
        conn.execute("DELETE FROM logs")
    return {"status": "ok"}
