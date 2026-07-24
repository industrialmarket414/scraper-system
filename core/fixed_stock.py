"""
Sabit stok yönetimi: Endüstriyel Market'in kendi deposundaki ürünlerin stoğu.

Mantık:
    EM Kodu → adet (int)
    B2B çekim sonrası, eşleştirilmiş EM kodu olan ürünlerin stok değerine
    bu sabit stok eklenir: final_stok = b2b_stok + sabit_stok.

Excel formatı (2 sütun):
    | EM Kodu | Sabit Stok |
    | EM-001  | 50         |
    | EM-002  | 12         |

Yükleme davranışı: TAMAMEN DEĞİŞTİR.
"""
import re
from io import BytesIO
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from core.database import get_conn


_BORDER = Border(
    left   = Side(style="thin", color="E2E8F0"),
    right  = Side(style="thin", color="E2E8F0"),
    top    = Side(style="thin", color="E2E8F0"),
    bottom = Side(style="thin", color="E2E8F0"),
)


# ── DB OPERASYONLARI ──────────────────────────────────────────────────────────
def load_all() -> dict[str, int]:
    """{em_code: qty} dict döndürür. Scheduler ürün enrichment'ında kullanır."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT em_code, qty FROM fixed_stock"
        ).fetchall()
    return {r["em_code"]: int(r["qty"]) for r in rows}


def replace_all(rows: list[tuple]) -> int:
    """Tüm sabit stoku siler, yeni satırları yazar. rows = [(em_code, qty), ...]"""
    with get_conn() as conn:
        conn.execute("DELETE FROM fixed_stock")
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO fixed_stock (em_code, qty, updated_at) "
                "VALUES (?, ?, datetime('now','localtime'))",
                rows,
            )
        conn.commit()
    return len(rows)


def upsert_one(em_code: str, qty: int) -> None:
    """Tek satır ekle/güncelle (manuel düzenleme için)."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO fixed_stock (em_code, qty, updated_at) "
            "VALUES (?, ?, datetime('now','localtime')) "
            "ON CONFLICT(em_code) DO UPDATE SET "
            "qty = excluded.qty, updated_at = excluded.updated_at",
            (em_code, qty),
        )
        conn.commit()


def delete_one(em_code: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM fixed_stock WHERE em_code = ?", (em_code,))
        conn.commit()


def get_summary() -> dict:
    with get_conn() as conn:
        cnt = conn.execute("SELECT COUNT(*) AS c FROM fixed_stock").fetchone()["c"]
        total = conn.execute(
            "SELECT COALESCE(SUM(qty), 0) AS s FROM fixed_stock"
        ).fetchone()["s"]
    return {"count": cnt, "total_qty": int(total)}


def get_all_rows() -> list[dict]:
    """Sayfa önizleme + dolu Excel için tüm kayıtları döndürür."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT em_code, qty, updated_at FROM fixed_stock "
            "ORDER BY em_code"
        ).fetchall()
    return [dict(r) for r in rows]


# ── EXCEL PARSE ───────────────────────────────────────────────────────────────
def parse_excel(content: bytes) -> tuple[list[tuple], dict]:
    """
    İlk sütun = EM Kodu, ikinci sütun = Sabit Stok (int).
    Header satırı atlanır. Boş veya negatif değer atlanır.
    Döndürür: (rows, info)
    """
    wb = load_workbook(BytesIO(content), data_only=True, read_only=True)
    ws = wb.active

    rows_iter = ws.iter_rows(values_only=True)
    header = next(rows_iter, None)  # header satırı atlanır

    rows: list[tuple] = []
    skipped: list[str] = []

    for row in rows_iter:
        if not row or len(row) < 2:
            continue
        em_cell, qty_cell = row[0], row[1]
        if em_cell is None:
            continue
        em_code = str(em_cell).strip()
        if not em_code:
            continue

        qty = _parse_int(qty_cell)
        if qty is None or qty < 0:
            skipped.append(f"{em_code}: '{qty_cell}'")
            continue

        rows.append((em_code, qty))

    return rows, {"total": len(rows), "skipped": skipped}


def _parse_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (ValueError, OverflowError):
            return None
    s = str(value).strip()
    if not s:
        return None
    # TR formatlı sayı (1.234 veya 1,234) → int
    m = re.match(r"^([\d.,]+)$", s)
    if not m:
        return None
    digits = m.group(1).replace(".", "").replace(",", "")
    try:
        return int(digits)
    except ValueError:
        return None


# ── STOK ENRICHMENT (scheduler kullanır) ──────────────────────────────────────
def parse_stock_int(s) -> int | None:
    """
    Çeşitli stok formatlarından int çıkar:
    - "34"          → 34
    - "999"         → 999
    - "1.234"       → 1234
    - "34 mevcut"   → 34
    - "Stok yok!"   → None
    - ""            → None
    """
    if s is None:
        return None
    text = str(s).strip()
    if not text:
        return None
    m = re.search(r"(\d[\d.,]*)", text)
    if not m:
        return None
    digits = m.group(1).replace(".", "").replace(",", "")
    try:
        return int(digits)
    except ValueError:
        return None


# ── EXCEL GENERATE ────────────────────────────────────────────────────────────
def _build_styled_sheet():
    """2-sütunlu (EM Kodu + Sabit Stok) stilize sheet."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Sabit Stok"

    # Header satırı
    em_cell = ws.cell(row=1, column=1, value="EM Kodu")
    em_cell.fill      = PatternFill("solid", fgColor="0F172A")
    em_cell.font      = Font(color="FFFFFF", bold=True, size=11)
    em_cell.alignment = Alignment(horizontal="center", vertical="center")
    em_cell.border    = _BORDER
    ws.column_dimensions["A"].width = 26

    qty_cell = ws.cell(row=1, column=2, value="Sabit Stok")
    qty_cell.fill      = PatternFill("solid", fgColor="15803D")  # yeşil
    qty_cell.font      = Font(color="FFFFFF", bold=True, size=11)
    qty_cell.alignment = Alignment(horizontal="center", vertical="center")
    qty_cell.border    = _BORDER
    ws.column_dimensions["B"].width = 16

    ws.row_dimensions[1].height = 26
    ws.freeze_panes = "A2"
    return wb, ws


def generate_template() -> bytes:
    """Boş şablon — 1 örnek satır."""
    wb, ws = _build_styled_sheet()

    body_fill = PatternFill("solid", fgColor="DCFCE7")  # açık yeşil
    em_fill   = PatternFill("solid", fgColor="F8FAFC")

    c = ws.cell(row=2, column=1, value="EM-ORNEK-001")
    c.font = Font(bold=True); c.border = _BORDER; c.fill = em_fill

    c = ws.cell(row=2, column=2, value=50)
    c.alignment = Alignment(horizontal="right", vertical="center")
    c.fill = body_fill; c.border = _BORDER

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def generate_filled_export() -> bytes:
    """DB'deki tüm sabit stok kayıtlarını renkli Excel olarak."""
    wb, ws = _build_styled_sheet()

    body_fill = PatternFill("solid", fgColor="DCFCE7")
    em_fill   = PatternFill("solid", fgColor="F8FAFC")

    rows = get_all_rows()
    for row_idx, r in enumerate(rows, 2):
        c = ws.cell(row=row_idx, column=1, value=r["em_code"])
        c.font = Font(bold=True); c.border = _BORDER; c.fill = em_fill
        c.alignment = Alignment(vertical="center")

        c = ws.cell(row=row_idx, column=2, value=r["qty"])
        c.alignment = Alignment(horizontal="right", vertical="center")
        c.fill = body_fill; c.border = _BORDER

    if rows:
        ws.auto_filter.ref = ws.dimensions

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
