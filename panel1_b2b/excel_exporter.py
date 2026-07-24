from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from pathlib import Path
from datetime import datetime

OUTPUT_DIR = Path(__file__).parent / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

_HDR_FILL = PatternFill("solid", fgColor="1E293B")
_HDR_FONT = Font(color="FFFFFF", bold=True, size=11)
_BORDER   = Border(
    left=Side(style="thin", color="D1D5DB"),
    right=Side(style="thin", color="D1D5DB"),
    top=Side(style="thin", color="D1D5DB"),
    bottom=Side(style="thin", color="D1D5DB"),
)
_ALT_FILL = PatternFill("solid", fgColor="F8FAFC")

# Varsayılan sütun tanımı (scraper COLUMNS tanımlamazsa kullanılır)
DEFAULT_COLUMNS: list[tuple] = [
    ("product_code", "Ürün Kodu", 18),
    ("product_name", "Ürün Adı",  40),
    ("brand",        "Marka",     20),
    ("stock",        "Stok",      12),
    ("price",        "Fiyat",     14),
]


def export_to_excel(
    site_name: str,
    site_id: str,
    products: list[dict],
    columns: list[tuple] | None = None,
) -> Path:
    # İlk sütun her zaman EM Kodu (mapping varsa dolar, yoksa boş kalır)
    cols = [("em_code", "EM Kodu", 20)] + list(columns or DEFAULT_COLUMNS)

    wb = Workbook()
    ws = wb.active
    ws.title = site_name[:31]  # Excel sheet adı max 31 karakter

    # ── Başlık satırı ────────────────────────────────────────────────────
    for col_idx, (_, header, width) in enumerate(cols, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.fill      = _HDR_FILL
        cell.font      = _HDR_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border    = _BORDER
        ws.column_dimensions[cell.column_letter].width = width

    ws.row_dimensions[1].height = 22

    # ── Veri satırları ───────────────────────────────────────────────────
    for row_idx, product in enumerate(products, 2):
        fill = _ALT_FILL if row_idx % 2 == 0 else None
        for col_idx, (key, _, _) in enumerate(cols, 1):
            cell = ws.cell(row=row_idx, column=col_idx,
                           value=product.get(key, ""))
            cell.border    = _BORDER
            cell.alignment = Alignment(horizontal="left", vertical="center")
            if fill:
                cell.fill = fill

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUTPUT_DIR / f"{site_id}_{timestamp}.xlsx"
    wb.save(path)
    return path
