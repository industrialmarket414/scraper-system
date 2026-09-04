import asyncio
from datetime import datetime
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from core.config_manager import load_settings
from core.log_manager import add_log

scheduler = AsyncIOScheduler(timezone="Europe/Istanbul")


# ── DB yardımcıları ───────────────────────────────────────────────────────────
def _save_history(site_id, site_name, trigger_type, status,
                  product_count=0, output_file=None, error_msg=None):
    from core.database import get_conn
    max_keep = load_settings().get("price_max_records", 30)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO b2b_scrape_history
               (site_id, site_name, trigger_type, status, product_count, output_file, error_msg)
               VALUES (?,?,?,?,?,?,?)""",
            (site_id, site_name, trigger_type, status, product_count, output_file, error_msg)
        )
        conn.execute(
            """DELETE FROM b2b_scrape_history WHERE site_id=? AND id NOT IN (
               SELECT id FROM b2b_scrape_history WHERE site_id=? ORDER BY scraped_at DESC LIMIT ?)""",
            (site_id, site_id, max_keep)
        )


def _save_b2b_stock(site_id, site_name, products):
    from core.database import get_conn
    max_records = load_settings().get("price_max_records", 30)
    with get_conn() as conn:
        for p in products:
            conn.execute(
                """INSERT INTO b2b_stock_records
                   (site_id, site_name, product_code, stock, adet,
                    price, price_text, discount_percent, discount_text)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (site_id, site_name,
                 p.get("product_code", ""), p.get("stock", ""), p.get("adet", ""),
                 p.get("price"), p.get("price_text", ""),
                 p.get("discount_percent"), p.get("discount_text", ""))
            )
        conn.execute(
            """DELETE FROM b2b_stock_records WHERE site_id=? AND id NOT IN (
               SELECT id FROM b2b_stock_records WHERE site_id=? ORDER BY scraped_at DESC LIMIT ?)""",
            (site_id, site_id, max_records)
        )


# ── Tek site çekimi ───────────────────────────────────────────────────────────
async def run_b2b_site(site_config: dict, trigger_type: str = "manual",
                        send_individual_mail: bool = True) -> tuple[int, str | None, str | None]:
    """
    Tek bir B2B sitesini çeker.
    send_individual_mail=False → toplu çekim modunda kullanılır,
                                  mail gönderimi run_scheduled_batch üstlenir.
    Döndürür: (ürün_sayısı, output_dosyası, hata_mesajı)
        hata_mesajı None ise başarılı, dolu string ise hata olmuştur.
    """
    from panel1_b2b.task_manager import is_running, register, unregister
    from core.database import get_conn

    site_id   = site_config["id"]
    site_name = site_config["name"]

    if is_running(site_id):
        add_log("panel1", site_id, site_name, "info",
                f"{site_name} — Çekim zaten devam ediyor, bu istek atlandı",
                notify=False)
        return 0, None

    task = asyncio.current_task()
    if task:
        register(site_id, task)

    output_path = None
    product_count = 0
    error_msg: str | None = None

    try:
        # Scraper bağımlılığı eksikse de hata bu görev içinde yakalanıp
        # panel geçmişine yazılsın; asyncio'da sahipsiz exception oluşmasın.
        from panel1_b2b.scrapers import get_scraper
        from panel1_b2b.excel_exporter import export_to_excel

        # Scraper manuel düğme ile mi yoksa zamanlayıcıyla mı açıldığını bilsin.
        # Özellikle Simtes, manuel çalışmada kullanıcı girişini beklerken
        # zamanlanmış çalışmada kayıtlı oturumu kullanır.
        runtime_site_config = dict(site_config)
        runtime_site_config["_trigger_type"] = trigger_type
        scraper  = get_scraper(runtime_site_config)
        products = await scraper.run()

        # EM (Endüstriyel Market) kodu ile zenginleştir — mapping varsa
        try:
            from core.product_mapping import load_mapping_for_site
            mapping = load_mapping_for_site(site_id)
            if mapping:
                for p in products:
                    code = (p.get("product_code") or "").strip()
                    p["em_code"] = mapping.get(code, "")
            else:
                for p in products:
                    p.setdefault("em_code", "")
        except Exception:
            # Mapping yüklemesi patlasa bile çekim akışı bozulmasın
            for p in products:
                p.setdefault("em_code", "")

        # Sabit stok ekle — em_code eşleşmiş ürünlerde final_stok = b2b + sabit
        try:
            from core.fixed_stock import load_all as load_fixed, parse_stock_int
            fixed = load_fixed()
            if fixed:
                for p in products:
                    em = (p.get("em_code") or "").strip()
                    if not em:
                        continue
                    extra = fixed.get(em)
                    if not extra or extra <= 0:
                        continue
                    cur = parse_stock_int(p.get("stock"))
                    if cur is None:
                        # Stok metinsel/parse edilemez → sadece sabit stok yaz
                        p["stock"] = str(extra)
                    else:
                        p["stock"] = str(cur + extra)
        except Exception:
            # Sabit stok yüklemesi patlasa bile çekim akışı bozulmasın
            pass

        output   = export_to_excel(site_name, site_id, products,
                                   columns=scraper.__class__.COLUMNS)
        _save_b2b_stock(site_id, site_name, products)
        _save_history(site_id, site_name, trigger_type, "success",
                      len(products), str(output))

        product_count = len(products)
        output_path   = str(output)

        # Bireysel mail — sadece manuel çekim veya toplu mod kapalıysa
        if send_individual_mail:
            settings   = load_settings()
            recipients = list(site_config.get("mail_recipients") or
                              settings.get("email", {}).get("recipients", []))
            mail_ok    = (site_config.get("mail_enabled", True) and
                          (site_config.get("mail_trigger", "auto") in ("auto", "both")
                           or trigger_type == "manual"))
            if recipients and mail_ok:
                from core.mail import send_mail
                send_mail(
                    recipients,
                    f"[ScrapeNet] {site_name} Stok Raporu",
                    f"<p><b>{site_name}</b> stok raporu ektedir.</p>"
                    f"<p>Toplam <b>{len(products)}</b> ürün çekildi.</p>",
                    attachments=[output],
                )

        with get_conn() as conn:
            conn.execute(
                """INSERT INTO b2b_jobs (site_id, last_run, last_status, output_file)
                   VALUES (?, datetime('now','localtime'), 'success', ?)
                   ON CONFLICT(site_id) DO UPDATE SET
                     last_run=datetime('now','localtime'),
                     last_status='success', output_file=excluded.output_file""",
                (site_id, str(output)),
            )

    except asyncio.CancelledError:
        add_log("panel1", site_id, site_name, "info",
                f"{site_name} — Çekim kullanıcı tarafından durduruldu", notify=False)
        _save_history(site_id, site_name, trigger_type, "error",
                      0, None, "Kullanıcı tarafından durduruldu")
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO b2b_jobs (site_id, last_run, last_status)
                   VALUES (?, datetime('now','localtime'), 'error')
                   ON CONFLICT(site_id) DO UPDATE SET
                     last_run=datetime('now','localtime'), last_status='error'""",
                (site_id,),
            )
        raise

    except Exception as e:
        error_msg = str(e) or e.__class__.__name__
        add_log("panel1", site_id, site_name, "error",
                f"{site_name} — Görev başarısız", error_msg, notify=True)
        _save_history(site_id, site_name, trigger_type, "error", 0, None, error_msg)
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO b2b_jobs (site_id, last_run, last_status)
                   VALUES (?, datetime('now','localtime'), 'error')
                   ON CONFLICT(site_id) DO UPDATE SET
                     last_run=datetime('now','localtime'), last_status='error'""",
                (site_id,),
            )

    finally:
        unregister(site_id)

    return product_count, output_path, error_msg


# ── Toplu zamanlanmış çekim ───────────────────────────────────────────────────
async def run_scheduled_batch():
    """
    b2b_schedule ayarındaki siteleri sırayla çeker.
    Hepsi bitince tek bir özet mail gönderir.
    """
    settings   = load_settings()
    b2b_sched  = settings.get("b2b_schedule", {})
    site_ids   = b2b_sched.get("enabled_sites", [])
    special_site_ids = {
        str(site_id)
        for site_id, special_time in (
            b2b_sched.get("site_times", {}) or {}
        ).items()
        if special_time
    }
    combined   = b2b_sched.get("combined_mail", True)
    b2b_sites  = settings.get("b2b_sites", [])

    # Seçili siteler (enabled_sites boşsa tüm aktif siteler)
    selected = [
        s for s in b2b_sites
        if (
            s.get("enabled", True)
            and (not site_ids or s["id"] in site_ids)
            and s["id"] not in special_site_ids
        )
    ]

    if not selected:
        add_log("panel1", "batch", "Toplu Çekim", "info",
                "Zamanlanmış çekim: çekilecek site bulunamadı", notify=False)
        return

    names = ", ".join(s["name"] for s in selected)
    add_log("panel1", "batch", "Toplu Çekim", "info",
            f"Zamanlanmış toplu çekim başladı: {names}", notify=False)

    # Her siteyi sırayla çek (bireysel mail gönderme)
    results: list[tuple[str, int, str | None, str | None]] = []
    for site_config in selected:
        try:
            count, output, err = await run_b2b_site(
                site_config, "auto", send_individual_mail=False
            )
            # err None değilse hata olmuş demektir — run_b2b_site exception'ı
            # yutuyor ama bize error_msg dönüyor
            results.append((site_config["name"], count, output, err))
        except asyncio.CancelledError:
            results.append((site_config["name"], 0, None, "İptal edildi"))
        except Exception as e:
            results.append(
                (site_config["name"], 0, None, str(e) or e.__class__.__name__)
            )

    # Toplu mail
    if combined and results:
        _send_combined_report(results, settings)

    # Panel 2 fiyat taramasını otomatik tetikle (settings ile kapatılabilir)
    if settings.get("auto_run_price_after_b2b", True):
        try:
            from panel2_fiyat.scheduler import run_all_price_sites
            add_log("panel2", "auto", "Otomatik Tetikleme", "info",
                    "B2B çekimi bitti → Panel 2 fiyat taraması başlatılıyor",
                    notify=False)
            await run_all_price_sites()
        except Exception as e:
            add_log("panel2", "auto", "Otomatik Tetikleme", "error",
                    "Panel 2 otomatik tetikleme başarısız", str(e), notify=True)


def _send_combined_report(results: list, settings: dict):
    """Tüm B2B çekimlerinin özet mailini tek seferinde gönderir."""
    from core.mail import send_mail

    recipients = settings.get("email", {}).get("recipients", [])
    if not recipients:
        return

    date_str = datetime.now().strftime("%d.%m.%Y %H:%M")

    rows_html  = ""
    total_prods = 0
    attachments = []
    error_count = 0
    warn_count  = 0   # ürün çekilemeyen (0 ürün, hata fırlatmadan) siteler

    for name, count, output_file, error in results:
        if error:
            # 1) Açık hata (crash, login fail, iptal) → KIRMIZI
            error_count += 1
            rows_html += f"""
            <tr>
                <td style="padding:9px 14px;border:1px solid #e2e8f0">{name}</td>
                <td style="padding:9px 14px;border:1px solid #e2e8f0;text-align:center">—</td>
                <td style="padding:9px 14px;border:1px solid #e2e8f0;color:#dc2626">
                    ✗ Hata: {error[:80]}
                </td>
            </tr>"""
        elif not count:
            # 2) Hata yok ama 0 ürün → sessiz başarısızlık → TURUNCU UYARI
            warn_count += 1
            rows_html += f"""
            <tr>
                <td style="padding:9px 14px;border:1px solid #e2e8f0">{name}</td>
                <td style="padding:9px 14px;border:1px solid #e2e8f0;text-align:center">0</td>
                <td style="padding:9px 14px;border:1px solid #e2e8f0;color:#d97706">
                    ⚠ Ürün çekilemedi (0 ürün)
                </td>
            </tr>"""
        else:
            # 3) Gerçek başarı (ürün çekildi) → YEŞİL
            total_prods += count
            rows_html += f"""
            <tr>
                <td style="padding:9px 14px;border:1px solid #e2e8f0">{name}</td>
                <td style="padding:9px 14px;border:1px solid #e2e8f0;text-align:center">
                    <b>{count}</b>
                </td>
                <td style="padding:9px 14px;border:1px solid #e2e8f0;color:#16a34a">
                    ✓ Başarılı
                </td>
            </tr>"""
            if output_file and Path(output_file).exists():
                attachments.append(Path(output_file))

    success_count = len(results) - error_count - warn_count
    sorun_parcalari = []
    if error_count:
        sorun_parcalari.append(f"{error_count} site hatalı")
    if warn_count:
        sorun_parcalari.append(f"{warn_count} site ürün çekemedi")
    hata_bilgisi = (", " + ", ".join(sorun_parcalari)) if sorun_parcalari else ""

    body = f"""
    <h2 style="color:#0f172a;margin:0 0 6px 0">B2B Stok Raporu</h2>
    <p style="color:#64748b;margin:0 0 18px 0">{date_str}</p>

    <p style="margin:0 0 16px 0">
        <b>{total_prods}</b> ürün çekildi —
        {success_count} site başarılı{hata_bilgisi}
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

    {'<p>Excel raporları ektedir.</p>' if attachments else ''}
    """

    try:
        send_mail(
            recipients,
            f"[ScrapeNet] B2B Stok Raporu — {date_str}",
            body,
            attachments=attachments or None,
        )
        add_log("panel1", "batch", "Toplu Mail", "info",
                f"Toplu B2B raporu gönderildi → {', '.join(recipients)}", notify=False)
    except Exception as e:
        add_log("panel1", "batch", "Toplu Mail", "error",
                "Toplu mail gönderilemedi", str(e), notify=True)


# ── APScheduler yönetimi ──────────────────────────────────────────────────────
def _load_jobs():
    settings  = load_settings()
    b2b_sched = settings.get("b2b_schedule", {})
    times     = b2b_sched.get("times", [])
    site_times = b2b_sched.get("site_times", {}) or {}

    if times:
        # Yeni sistem: toplu çekim işi
        for i, time_str in enumerate(times[:2]):
            try:
                h, m = time_str.split(":")
                scheduler.add_job(
                    run_scheduled_batch,
                    CronTrigger(hour=int(h), minute=int(m), timezone="Europe/Istanbul"),
                    id=f"b2b_batch_{i}",
                    replace_existing=True,
                )
            except Exception:
                pass

        # Uzun süren siteler toplu çekimden ayrılıp kendi saatinde çalışabilir.
        sites_by_id = {
            site["id"]: site
            for site in settings.get("b2b_sites", [])
            if site.get("enabled", True)
        }
        for site_id, configured_times in site_times.items():
            site = sites_by_id.get(site_id)
            if not site:
                continue
            if isinstance(configured_times, str):
                configured_times = [configured_times]
            for i, time_str in enumerate((configured_times or [])[:2]):
                try:
                    h, m = time_str.split(":")
                    scheduler.add_job(
                        run_b2b_site,
                        CronTrigger(
                            hour=int(h),
                            minute=int(m),
                            timezone="Europe/Istanbul",
                        ),
                        args=[site, "auto"],
                        id=f"b2b_special_{site_id}_{i}",
                        replace_existing=True,
                    )
                except Exception:
                    pass
    else:
        # Geriye dönük uyumluluk: per-site schedule
        for site in settings.get("b2b_sites", []):
            if not site.get("enabled", True):
                continue
            sc = site.get("schedule_config", {})
            try:
                t = sc.get("type", "daily")
                if t == "daily":
                    h2, m2 = sc.get("daily_time", "08:00").split(":")
                    trigger = CronTrigger(hour=int(h2), minute=int(m2), timezone="Europe/Istanbul")
                else:
                    trigger = CronTrigger.from_crontab(
                        site.get("schedule", "0 8 * * *"), timezone="Europe/Istanbul"
                    )
                scheduler.add_job(
                    run_b2b_site, trigger,
                    args=[site, "auto"],
                    id=f"b2b_{site['id']}",
                    replace_existing=True,
                )
            except Exception:
                pass


def start_scheduler():
    _load_jobs()
    if not scheduler.running:
        scheduler.start()


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)


def reload_scheduler():
    scheduler.remove_all_jobs()
    _load_jobs()
