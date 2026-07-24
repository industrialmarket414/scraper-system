"""
Ürün kodu eşleştirme: Endüstriyel Market kodu ↔ her B2B'nin kendi kodu.

Excel formatı:
    | EM Kodu | NOAS | Yıldız SDE | Kıraç Elektrik | ... |
    | EM-001  | A-1  | YA         | KRC-9          | ... |
    | EM-002  |      | YB         |                | ... |

Yükleme davranışı: TAMAMEN DEĞİŞTİR — yeni Excel yüklenince DB'deki tüm
eşleştirmeler silinir, sadece yeni dosyadaki kayıtlar kalır.

Header eşleştirme: header text → site.id veya site.name (case-insensitive).
İlk sütun her zaman EM kodudur (header'a bakılmaz).
"""
from io import BytesIO
from pathlib import Path
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from core.database import get_conn


# ── Stil paleti — her site sütununa farklı renk ───────────────────────────────
# Tuple: (header_bg_hex, body_bg_hex)
_SITE_PALETTE: list[tuple[str, str]] = [
    ("1E40AF", "DBEAFE"),  # mavi
    ("BE185D", "FCE7F3"),  # pembe
    ("15803D", "DCFCE7"),  # yeşil
    ("B45309", "FEF3C7"),  # amber
    ("6D28D9", "EDE9FE"),  # mor
    ("0E7490", "CFFAFE"),  # cyan
    ("9F1239", "FFE4E6"),  # gül
    ("4D7C0F", "ECFCCB"),  # lime
    ("7C2D12", "FED7AA"),  # turuncu
    ("1F2937", "F3F4F6"),  # slate
]

_THIN_BORDER = Border(
    left   = Side(style="thin", color="E2E8F0"),
    right  = Side(style="thin", color="E2E8F0"),
    top    = Side(style="thin", color="E2E8F0"),
    bottom = Side(style="thin", color="E2E8F0"),
)


# ── Excel parser ──────────────────────────────────────────────────────────────
def parse_excel(content: bytes, sites: list[dict]) -> tuple[list[tuple], dict]:
    """
    Excel içeriğini parse eder.
    Döndürür: (rows, info)
        rows = [(em_code, site_id, site_code), ...]
        info = {"total": N, "by_site": {site_id: count}, "skipped_headers": [...]}
    """
    wb = load_workbook(BytesIO(content), data_only=True, read_only=True)
    ws = wb.active

    # Site lookup: ad/id (lower) → site_id
    site_lookup: dict[str, str] = {}
    for s in sites:
        site_lookup[s["id"].strip().lower()] = s["id"]
        site_lookup[s["name"].strip().lower()] = s["id"]

    rows_iter = ws.iter_rows(values_only=True)
    header = next(rows_iter, None)
    if not header:
        return [], {"total": 0, "by_site": {}, "skipped_headers": []}

    # Header'dan site_id eşle (1. sütun = EM kodu, atlanır)
    col_to_site: dict[int, str] = {}
    skipped: list[str] = []
    for idx, cell in enumerate(header):
        if idx == 0:
            continue
        if cell is None or str(cell).strip() == "":
            continue
        key = str(cell).strip().lower()
        sid = site_lookup.get(key)
        if sid:
            col_to_site[idx] = sid
        else:
            skipped.append(str(cell))

    rows: list[tuple] = []
    by_site: dict[str, int] = {}

    for row in rows_iter:
        if not row:
            continue
        em_cell = row[0]
        if em_cell is None:
            continue
        em_code = str(em_cell).strip()
        if not em_code:
            continue

        for col_idx, sid in col_to_site.items():
            if col_idx >= len(row):
                continue
            cell = row[col_idx]
            if cell is None:
                continue
            site_code = str(cell).strip()
            if not site_code:
                continue
            rows.append((em_code, sid, site_code))
            by_site[sid] = by_site.get(sid, 0) + 1

    return rows, {
        "total": len(rows),
        "by_site": by_site,
        "skipped_headers": skipped,
    }


# ── DB operasyonları ──────────────────────────────────────────────────────────
def replace_all(rows: list[tuple]) -> int:
    """Tüm mevcut mapping'i siler, yeni satırları yazar. Döndürür: yazılan satır sayısı."""
    with get_conn() as conn:
        conn.execute("DELETE FROM product_mapping")
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO product_mapping "
                "(em_code, site_id, site_code) VALUES (?, ?, ?)",
                rows,
            )
        conn.commit()
    return len(rows)


def load_mapping_for_site(site_id: str) -> dict[str, str]:
    """
    Bir site için {site_code: em_code} dict döndürür.
    Scrape sonrası ürün enrichment'ında kullanılır.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT site_code, em_code FROM product_mapping WHERE site_id = ?",
            (site_id,),
        )
        return {r["site_code"]: r["em_code"] for r in cur.fetchall()}


def get_summary() -> dict:
    """Panel için: site bazında satır sayısı + toplam."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT site_id, COUNT(*) AS cnt FROM product_mapping GROUP BY site_id"
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM product_mapping"
        ).fetchone()["c"]
        unique_em = conn.execute(
            "SELECT COUNT(DISTINCT em_code) AS c FROM product_mapping"
        ).fetchone()["c"]
    return {
        "total":      total,
        "unique_em":  unique_em,
        "by_site":    {r["site_id"]: r["cnt"] for r in rows},
    }


def get_sample(limit: int = 50) -> list[dict]:
    """Panel için: ilk N satır önizleme."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT em_code, site_id, site_code FROM product_mapping "
            "ORDER BY em_code, site_id LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Ortak: header'ları stilize et, site_id → (col, body_fill) dict döndür ────
def _build_styled_sheet(sites: list[dict]):
    """
    Yeni workbook + stilize edilmiş header satırı oluşturur.
    Döndürür: (wb, ws, site_col_map)
        site_col_map = {site_id: (column_index, body_PatternFill)}
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Eşleştirme"

    # 1. sütun: EM Kodu — koyu zemin
    em_cell = ws.cell(row=1, column=1, value="EM Kodu")
    em_cell.fill      = PatternFill("solid", fgColor="0F172A")
    em_cell.font      = Font(color="FFFFFF", bold=True, size=11)
    em_cell.alignment = Alignment(horizontal="center", vertical="center")
    em_cell.border    = _THIN_BORDER
    ws.column_dimensions["A"].width = 24

    # Site sütunları — her birine paletten bir renk
    site_col_map: dict[str, tuple[int, PatternFill]] = {}
    for i, site in enumerate(sites):
        col = i + 2
        hdr_hex, body_hex = _SITE_PALETTE[i % len(_SITE_PALETTE)]
        cell = ws.cell(row=1, column=col, value=site["name"])
        cell.fill      = PatternFill("solid", fgColor=hdr_hex)
        cell.font      = Font(color="FFFFFF", bold=True, size=11)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border    = _THIN_BORDER
        ws.column_dimensions[cell.column_letter].width = 20
        site_col_map[site["id"]] = (col, PatternFill("solid", fgColor=body_hex))

    ws.row_dimensions[1].height = 26
    ws.freeze_panes = "B2"

    return wb, ws, site_col_map


# ── Template generator (boş) ──────────────────────────────────────────────────
def generate_template(sites: list[dict]) -> bytes:
    """Boş şablon — stilize header + 1 örnek satır."""
    wb, ws, site_col_map = _build_styled_sheet(sites)

    # Örnek satır
    c = ws.cell(row=2, column=1, value="EM-ORNEK-001")
    c.font   = Font(bold=True)
    c.border = _THIN_BORDER
    for sid, (col, body_fill) in site_col_map.items():
        cell = ws.cell(row=2, column=col, value="")
        cell.fill   = body_fill
        cell.border = _THIN_BORDER

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Dolu export — DB'deki tüm mapping'i renkli Excel olarak ──────────────────
def generate_filled_export(sites: list[dict]) -> bytes:
    """
    DB'deki tüm eşleştirmeleri site sütunlarında pivot eder.
    Her site sütunu farklı renkte, her EM kodu için 1 satır.
    """
    wb, ws, site_col_map = _build_styled_sheet(sites)

    # DB'den çek + pivot {em_code: {site_id: site_code}}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT em_code, site_id, site_code FROM product_mapping "
            "ORDER BY em_code, site_id"
        ).fetchall()

    pivot: dict[str, dict[str, str]] = {}
    for r in rows:
        pivot.setdefault(r["em_code"], {})[r["site_id"]] = r["site_code"]

    # Satırları yaz (alfabetik EM kodu sırası)
    for row_idx, em_code in enumerate(sorted(pivot.keys()), 2):
        c = ws.cell(row=row_idx, column=1, value=em_code)
        c.font      = Font(bold=True)
        c.alignment = Alignment(vertical="center")
        c.border    = _THIN_BORDER

        site_codes = pivot[em_code]
        for sid, (col, body_fill) in site_col_map.items():
            cell = ws.cell(
                row=row_idx, column=col,
                value=site_codes.get(sid, ""),
            )
            cell.fill      = body_fill
            cell.alignment = Alignment(vertical="center")
            cell.border    = _THIN_BORDER

    if pivot:
        ws.auto_filter.ref = ws.dimensions

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
