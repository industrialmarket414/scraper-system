from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from core.config_manager import load_settings
from core.log_manager import add_log

scheduler = AsyncIOScheduler(timezone="Europe/Istanbul")


async def run_all_price_sites():
    from panel2_fiyat.scrapers import get_scraper
    from panel2_fiyat.database import save_products
    from core.mail import send_error_mail

    for site in load_settings().get("price_sites", []):
        if not site.get("enabled", True):
            continue
        try:
            scraper = get_scraper(site)
            products = await scraper.run()
            save_products(site["id"], site["name"], products)
            if getattr(scraper, "EXPORT_EXCEL", False):
                from panel2_fiyat.excel_exporter import export_price_excel
                output = export_price_excel(
                    site["name"], site["id"], products,
                    columns=getattr(scraper, "EXCEL_COLUMNS", None),
                )
                add_log("panel2", site["id"], site["name"], "success",
                        f"Excel oluşturuldu: {output.name}")
        except Exception as e:
            # notify=True → tek hata maili add_log üzerinden gönderilir
            add_log("panel2", site["id"], site["name"], "error",
                    "Zamanlı tarama başarısız", str(e), notify=True)


async def run_single_price_site(site_config: dict):
    from panel2_fiyat.scrapers import get_scraper
    from panel2_fiyat.database import save_products

    scraper = get_scraper(site_config)
    products = await scraper.run()
    save_products(site_config["id"], site_config["name"], products)
    if getattr(scraper, "EXPORT_EXCEL", False):
        from panel2_fiyat.excel_exporter import export_price_excel
        output = export_price_excel(
            site_config["name"], site_config["id"], products,
            columns=getattr(scraper, "EXCEL_COLUMNS", None),
        )
        add_log("panel2", site_config["id"], site_config["name"], "success",
                f"Excel oluşturuldu: {output.name}")


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
