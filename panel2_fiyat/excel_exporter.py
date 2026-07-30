"""Rakip fiyat siteleri için firma bazlı Excel çıktısı."""
from datetime import datetime
from pathlib import Path
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.table import Table, TableStyleInfo


OUTPUT_DIR = Path(__file__).parent / "outputs"

DEFAULT_COLUMNS = [
    ("brand", "Marka", 18),
    ("product_code", "Ürün Kodu", 20),
    ("product_name", "Ürün Adı", 52),
    ("stock", "Stok Durumu", 14),
    ("discount_percent", "İskonto %", 13),
    ("list_price", "Liste Fiyatı", 16),
    ("price", "Satış Fiyatı", 16),
    ("category", "Kategori", 25),
    ("url", "Ürün Linki", 45),
    ("scraped_at", "Çekim Tarihi", 20),
]

HEADER_FILL = PatternFill("solid", fgColor="0F4C5C")
HEADER_FONT = Font(color="FFFFFF", bold=True)
IN_STOCK_FILL = PatternFill("solid", fgColor="DCFCE7")
OUT_STOCK_FILL = PatternFill("solid", fgColor="FEE2E2")


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip().lower())
    return cleaned.strip("_") or "firma"


def _safe_sheet_title(value: str) -> str:
    title = re.sub(r"[\[\]:*?/\\]+", "-", value).strip()
    return (title or "Ürünler")[:31]


def export_price_excel(
    site_name: str,
    site_id: str,
    products: list[dict],
    columns: list[tuple] | None = None,
) -> Path:
    columns = list(columns or DEFAULT_COLUMNS)
    company_dir = OUTPUT_DIR / _safe_name(site_id)
    company_dir.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = _safe_sheet_title(site_name)
    sheet.sheet_view.showGridLines = False

    for column_index, (_, header, width) in enumerate(columns, 1):
        cell = sheet.cell(row=1, column=column_index, value=header)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        sheet.column_dimensions[cell.column_letter].width = width
    sheet.row_dimensions[1].height = 24

    key_to_column = {
        key: column_index
        for column_index, (key, _, _) in enumerate(columns, 1)
    }
    for row_index, product in enumerate(products, 2):
        for column_index, (key, _, _) in enumerate(columns, 1):
            value = product.get(key)
            cell = sheet.cell(row=row_index, column=column_index, value=value)
            cell.alignment = Alignment(
                vertical="center",
                wrap_text=key in {"product_name", "category"},
            )
            if key in {"list_price", "price"} and isinstance(value, (int, float)):
                cell.number_format = '#,##0.00 "₺"'
            elif key == "stock_quantity" and isinstance(value, (int, float)):
                cell.number_format = "#,##0.00##"
            elif key == "discount_percent" and isinstance(value, (int, float)):
                # Veride 64 değeri %64 anlamındadır; Excel yüzde formatı 0,64 bekler.
                cell.value = value / 100
                cell.number_format = "0.00%"
            elif key == "url" and value:
                cell.hyperlink = str(value)
                cell.style = "Hyperlink"

        stock_column = key_to_column.get("stock")
        if stock_column:
            stock_cell = sheet.cell(row=row_index, column=stock_column)
            stock_cell.fill = (
                IN_STOCK_FILL if stock_cell.value == "Stokta" else OUT_STOCK_FILL
            )
        sheet.row_dimensions[row_index].height = 30

    last_row = max(1, len(products) + 1)
    last_column = sheet.cell(row=1, column=len(columns)).column_letter
    if products:
        table = Table(
            displayName=f"{_safe_name(site_id).title().replace('_', '')}Products",
            ref=f"A1:{last_column}{last_row}",
        )
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        sheet.add_table(table)

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{last_column}{last_row}"

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = company_dir / f"{_safe_name(site_id)}_{timestamp}.xlsx"
    workbook.save(output_path)
    return output_path


def latest_output(site_id: str) -> Path | None:
    company_dir = OUTPUT_DIR / _safe_name(site_id)
    files = sorted(company_dir.glob("*.xlsx"), key=lambda path: path.stat().st_mtime)
    return files[-1] if files else None
