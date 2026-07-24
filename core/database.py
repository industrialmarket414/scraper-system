import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "config" / "scrapenet.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _migrate_add_columns(conn, table: str, columns: list[tuple]):
    """
    SQLite ALTER TABLE ADD COLUMN — sadece eksik sütunları ekler.
    columns = [(col_name, col_definition), ...]
    """
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col_name, col_def in columns:
        if col_name not in existing:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")
            except Exception:
                pass


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS logs (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                panel     TEXT NOT NULL,
                site_id   TEXT,
                site_name TEXT,
                status    TEXT NOT NULL,
                message   TEXT,
                details   TEXT
            );

            -- Piyasa fiyat kayıtları (Panel 2)
            CREATE TABLE IF NOT EXISTS price_records (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id           TEXT NOT NULL,
                site_name         TEXT NOT NULL,
                product_code      TEXT,
                product_name      TEXT NOT NULL DEFAULT '',
                brand             TEXT,
                category          TEXT,
                price             REAL,
                old_price         REAL,
                list_price        REAL,
                discount_percent  REAL,
                currency          TEXT DEFAULT 'TRY',
                is_bestseller     INTEGER DEFAULT 0,
                bestseller_label  TEXT,
                em_code           TEXT,
                url               TEXT,
                scraped_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_price_site    ON price_records(site_id);
            CREATE INDEX IF NOT EXISTS idx_price_code    ON price_records(product_code);
            CREATE INDEX IF NOT EXISTS idx_price_scraped ON price_records(scraped_at);
            -- idx_price_em ve idx_price_bs index'leri migration sonrası oluşturulur

            -- B2B stok kayıtları (Panel 1 → Panel 2 araması)
            CREATE TABLE IF NOT EXISTS b2b_stock_records (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id          TEXT NOT NULL,
                site_name        TEXT NOT NULL,
                product_code     TEXT,
                stock            TEXT,
                adet             TEXT,
                price            REAL,
                price_text       TEXT,
                discount_percent REAL,
                discount_text    TEXT,
                scraped_at       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_b2b_site    ON b2b_stock_records(site_id);
            CREATE INDEX IF NOT EXISTS idx_b2b_code    ON b2b_stock_records(product_code);
            CREATE INDEX IF NOT EXISTS idx_b2b_scraped ON b2b_stock_records(scraped_at);

            -- B2B çekim geçmişi (son 30 per site)
            CREATE TABLE IF NOT EXISTS b2b_scrape_history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id       TEXT NOT NULL,
                site_name     TEXT NOT NULL,
                scraped_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                trigger_type  TEXT NOT NULL DEFAULT 'manual',
                status        TEXT NOT NULL,
                product_count INTEGER DEFAULT 0,
                output_file   TEXT,
                error_msg     TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_history_site    ON b2b_scrape_history(site_id);
            CREATE INDEX IF NOT EXISTS idx_history_scraped ON b2b_scrape_history(scraped_at);

            -- AI analiz raporları
            CREATE TABLE IF NOT EXISTS ai_reports (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                report_type   TEXT NOT NULL DEFAULT 'full',
                input_summary TEXT,
                report_text   TEXT NOT NULL,
                token_count   INTEGER
            );

            -- Panel 1: en son iş durumu
            CREATE TABLE IF NOT EXISTS b2b_jobs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id     TEXT NOT NULL UNIQUE,
                last_run    TEXT,
                last_status TEXT,
                output_file TEXT
            );

            -- Endüstriyel Market ↔ B2B ürün kodu eşleştirmesi
            CREATE TABLE IF NOT EXISTS product_mapping (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                em_code   TEXT NOT NULL,
                site_id   TEXT NOT NULL,
                site_code TEXT NOT NULL,
                UNIQUE (site_id, site_code)
            );
            CREATE INDEX IF NOT EXISTS idx_pm_site_code ON product_mapping(site_id, site_code);
            CREATE INDEX IF NOT EXISTS idx_pm_em_code   ON product_mapping(em_code);

            -- Endüstriyel Market kendi deposundaki sabit stoklar
            CREATE TABLE IF NOT EXISTS fixed_stock (
                em_code     TEXT PRIMARY KEY,
                qty         INTEGER NOT NULL DEFAULT 0,
                updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
        """)

        # ── price_records eski tablo migrasyonu (eksik sütunları ekle) ──
        _migrate_add_columns(conn, "price_records", [
            ("category",         "TEXT"),
            ("list_price",       "REAL"),
            ("currency",         "TEXT DEFAULT 'TRY'"),
            ("is_bestseller",    "INTEGER DEFAULT 0"),
            ("bestseller_label", "TEXT"),
            ("em_code",          "TEXT"),
        ])
        # Yeni indeksler — _migrate sonrası
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_price_em ON price_records(em_code)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_price_bs ON price_records(is_bestseller)")
        except Exception:
            pass
