from datetime import datetime
import os
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from core.config_manager import load_settings
from core.log_manager import add_log
from panel2_fiyat.cancel import (
    ScrapeCancelled,
    clear_cancel,
    is_cancel_requested,
    request_cancel,
)


scheduler = AsyncIOScheduler(timezone="Europe/Istanbul")
_run_status: dict[str, dict] = {}
_FRESH_RUN_ENV_KEYS = {
    "AYDINLATMADUNYAM_MAX_PRODUCTS",
    "AYDINLATMADUNYAM_RESUME_CACHE",
    "BUYUKALPLER_CACHE",
    "BUYUKALPLER_MAX_PRODUCTS",
    "CIMENELEKTRIK_CACHE",
    "CIMENELEKTRIK_MAX_PRODUCTS",
    "ELEKTRIKCIDEN_MAX_PRODUCTS",
    "ELEKTRIKMALZEMESI_MAX_CATEGORIES",
    "ELEKTRIKMALZEMESI_MAX_DETAILS",
    "ELEKTRIKMALZEMESI_MAX_PAGES",
    "ELEKTRIKMALZEMESI_STOCK_CACHE",
    "ELEKTRIKMARKET_MAX_PAGES",
    "ELEKTROMARKETIM_MAX_PAGES",
    "ELEKTRIX_MAX_PAGES",
    "KOZAYDINLATMA_CACHE",
    "KRC_ELEKTROMARKET_MAX_PRODUCTS",
    "STARAKIM_MAX_PAGES",
    "TUMPA_MAX_CATEGORIES",
    "TUMPA_MAX_PAGES",
    "UCFAZ_MAX_PRODUCTS",
    "ZEYBEK_MAX_PRODUCTS",
}
_LONG_RUNNING_AFTER_SECONDS = 20 * 60


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _status_with_elapsed(status: dict) -> dict:
    status = dict(status)
    started_at = status.get("started_at")
    if started_at:
        try:
            started = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")
            elapsed = max(0, int((datetime.now() - started).total_seconds()))
            status["elapsed_seconds"] = elapsed
            status["elapsed_minutes"] = elapsed // 60
            if status.get("running") and elapsed >= _LONG_RUNNING_AFTER_SECONDS:
                status["long_running"] = True
                status["message"] = (
                    f"Guncel veri cekiliyor ({elapsed // 60} dk). "
                    "Site yavas olabilir."
                )
            else:
                status["long_running"] = False
        except ValueError:
            status["elapsed_seconds"] = None
            status["elapsed_minutes"] = None
            status["long_running"] = False
    else:
        status["elapsed_seconds"] = None
        status["elapsed_minutes"] = None
        status["long_running"] = False
    return status


def get_run_status(site_id: str | None = None):
    if site_id:
        return _status_with_elapsed(_run_status.get(site_id, {
            "site_id": site_id,
            "running": False,
            "status": "idle",
        }))
    return {
        current_site_id: _status_with_elapsed(status)
        for current_site_id, status in _run_status.items()
    }


def _mark_running(site: dict):
    _run_status[site["id"]] = {
        "site_id": site["id"],
        "site_name": site.get("name", site["id"]),
        "running": True,
        "status": "running",
        "started_at": _now_text(),
        "finished_at": None,
        "product_count": None,
        "output": None,
        "message": "Guncel veri cekiliyor",
        "error": None,
    }


def _mark_success(site: dict, product_count: int, output=None):
    _run_status[site["id"]] = {
        **_run_status.get(site["id"], {}),
        "site_id": site["id"],
        "site_name": site.get("name", site["id"]),
        "running": False,
        "status": "success",
        "finished_at": _now_text(),
        "product_count": product_count,
        "output": str(output) if output else None,
        "message": f"{product_count} urun cekildi",
        "error": None,
    }


def _mark_error(site: dict, exc: Exception):
    _run_status[site["id"]] = {
        **_run_status.get(site["id"], {}),
        "site_id": site["id"],
        "site_name": site.get("name", site["id"]),
        "running": False,
        "status": "error",
        "finished_at": _now_text(),
        "message": "Tarama basarisiz",
        "error": str(exc),
    }


def clear_run_status(site_id: str):
    request_cancel(site_id)
    current = get_run_status(site_id)
    _run_status[site_id] = {
        **current,
        "site_id": site_id,
        "running": False,
        "status": "stopped",
        "finished_at": _now_text(),
        "message": "Kullanici tarafindan durduruldu",
        "error": None,
    }
    return get_run_status(site_id)


def _send_price_summary_mail(results: list[dict]):
    settings = load_settings()
    recipients = list(settings.get("email", {}).get("recipients", []))
    if not recipients:
        return

    success = [item for item in results if item.get("status") == "success"]
    failed = [item for item in results if item.get("status") == "error"]
    stopped = [item for item in results if item.get("status") == "stopped"]
    attachments = [
        Path(item["output"])
        for item in success
        if item.get("output") and Path(item["output"]).exists()
    ]

    date_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    total_products = sum(int(item.get("product_count") or 0) for item in success)
    rows_html = ""
    for item in results:
        site_name = item.get("site_name") or item.get("site_id")
        count = int(item.get("product_count") or 0)
        if item.get("status") == "success":
            rows_html += f"""
            <tr>
                <td style="padding:9px 14px;border:1px solid #e2e8f0">{site_name}</td>
                <td style="padding:9px 14px;border:1px solid #e2e8f0;text-align:center">
                    <b>{count}</b>
                </td>
                <td style="padding:9px 14px;border:1px solid #e2e8f0;color:#16a34a">
                    ✓ Başarılı
                </td>
            </tr>"""
        elif item.get("status") == "stopped":
            rows_html += f"""
            <tr>
                <td style="padding:9px 14px;border:1px solid #e2e8f0">{site_name}</td>
                <td style="padding:9px 14px;border:1px solid #e2e8f0;text-align:center">—</td>
                <td style="padding:9px 14px;border:1px solid #e2e8f0;color:#d97706">
                    ⚠ Durduruldu
                </td>
            </tr>"""
        else:
            error = item.get("error") or item.get("message") or "Bilinmeyen hata"
            rows_html += f"""
            <tr>
                <td style="padding:9px 14px;border:1px solid #e2e8f0">{site_name}</td>
                <td style="padding:9px 14px;border:1px solid #e2e8f0;text-align:center">—</td>
                <td style="padding:9px 14px;border:1px solid #e2e8f0;color:#dc2626">
                    ✗ Hata: {error[:120]}
                </td>
            </tr>"""

    body = f"""
    <h2 style="color:#0f172a;margin:0 0 6px 0">Rakip Fiyat Raporu</h2>
    <p style="color:#64748b;margin:0 0 18px 0">{date_str}</p>

    <p style="margin:0 0 16px 0">
        <b>{total_products}</b> ürün çekildi —
        {len(success)} firma başarılı
        {f", {len(failed)} firma hatalı" if failed else ""}
        {f", {len(stopped)} firma durduruldu" if stopped else ""}
    </p>

    <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:16px">
        <thead>
            <tr style="background:#f8fafc">
                <th style="padding:10px 14px;border:1px solid #e2e8f0;text-align:left">Site</th>
                <th style="padding:10px 14px;border:1px solid #e2e8f0;text-align:center">Ürün Sayısı</th>
                <th style="padding:10px 14px;border:1px solid #e2e8f0;text-align:left">Durum</th>
            </tr>
        </thead>
        <tbody>{rows_html}</tbody>
    </table>

    {'<p>Rakip firma Excel raporları ektedir.</p>' if attachments else ''}
    """
    try:
        from core.mail import send_mail
        send_mail(
            recipients,
            f"[ScrapeNet] Rakip Fiyat Raporu — {date_str}",
            body,
            attachments=attachments,
        )
        add_log(
            "panel2",
            "mail",
            "Rakip Fiyat Mail",
            "info",
            f"Rakip fiyat Excel ozeti gonderildi: {', '.join(recipients)}",
            notify=False,
        )
    except Exception as exc:
        add_log(
            "panel2",
            "mail",
            "Rakip Fiyat Mail",
            "error",
            "Rakip fiyat Excel ozeti gonderilemedi",
            str(exc),
            notify=False,
        )


def _send_single_price_excel_mail(site: dict, product_count: int, output: Path | None):
    if not output or not Path(output).exists():
        return
    settings = load_settings()
    recipients = list(
        site.get("mail_recipients") or settings.get("email", {}).get("recipients", [])
    )
    if not recipients:
        return

    site_name = site.get("name", site["id"])
    try:
        from core.mail import send_mail

        send_mail(
            recipients,
            f"[ScrapeNet] {site_name} Rakip Fiyat Raporu",
            (
                f"<p><b>{site_name}</b> rakip fiyat raporu ektedir.</p>"
                f"<p>Toplam <b>{product_count}</b> urun cekildi.</p>"
            ),
            attachments=[Path(output)],
        )
        add_log(
            "panel2",
            site["id"],
            site_name,
            "success",
            f"Manuel rakip fiyat maili gonderildi: {Path(output).name}",
            notify=False,
        )
    except Exception as exc:
        add_log(
            "panel2",
            site["id"],
            site_name,
            "error",
            "Manuel rakip fiyat maili gonderilemedi",
            str(exc),
            notify=False,
        )


async def _run_price_site(
    site: dict,
    notify_errors: bool = False,
    send_individual_mail: bool = False,
):
    old_env = {}
    try:
        from panel2_fiyat.database import save_products
        from panel2_fiyat.scrapers import get_scraper

        clear_cancel(site["id"])
        _mark_running(site)
        old_env = {
            key: os.environ.pop(key)
            for key in _FRESH_RUN_ENV_KEYS
            if key in os.environ
        }
        scraper = get_scraper(site)
        scraper.should_stop = lambda: is_cancel_requested(site["id"])
        products = await scraper.run()
        if is_cancel_requested(site["id"]):
            raise ScrapeCancelled("Kullanici tarafindan durduruldu")
        save_products(site["id"], site["name"], products)
        output = None
        if getattr(scraper, "EXPORT_EXCEL", False):
            from panel2_fiyat.excel_exporter import export_price_excel
            output = export_price_excel(
                site["name"],
                site["id"],
                products,
                columns=getattr(scraper, "EXCEL_COLUMNS", None),
            )
            add_log(
                "panel2",
                site["id"],
                site["name"],
                "success",
                f"Excel olusturuldu: {output.name}",
            )
        if send_individual_mail:
            _send_single_price_excel_mail(site, len(products), output)
        _mark_success(site, len(products), output)
        return products
    except ScrapeCancelled as exc:
        clear_run_status(site["id"])
        add_log(
            "panel2",
            site["id"],
            site["name"],
            "warning",
            "Tarama durduruldu",
            str(exc),
            notify=False,
        )
        raise
    except Exception as exc:
        _mark_error(site, exc)
        add_log(
            "panel2",
            site["id"],
            site["name"],
            "error",
            "Tarama basarisiz",
            str(exc),
            notify=notify_errors,
        )
        raise
    finally:
        if old_env:
            os.environ.update(old_env)


async def run_all_price_sites():
    results = []
    for site in load_settings().get("price_sites", []):
        if not site.get("enabled", True):
            continue
        try:
            await _run_price_site(site, notify_errors=True)
            results.append(get_run_status(site["id"]))
        except ScrapeCancelled:
            results.append(get_run_status(site["id"]))
        except Exception:
            results.append(get_run_status(site["id"]))
            continue
    if results:
        _send_price_summary_mail(results)


async def run_single_price_site(site_config: dict):
    return await _run_price_site(
        site_config,
        notify_errors=False,
        send_individual_mail=True,
    )


def start_scheduler():
    cron = load_settings().get("price_schedule", "0 */6 * * *")
    scheduler.add_job(
        run_all_price_sites,
        CronTrigger.from_crontab(cron, timezone="Europe/Istanbul"),
        id="price_all",
        replace_existing=True,
    )
    if not scheduler.running:
        scheduler.start()


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)


def reload_scheduler():
    scheduler.remove_all_jobs()
    cron = load_settings().get("price_schedule", "0 */6 * * *")
    scheduler.add_job(
        run_all_price_sites,
        CronTrigger.from_crontab(cron, timezone="Europe/Istanbul"),
        id="price_all",
        replace_existing=True,
    )
