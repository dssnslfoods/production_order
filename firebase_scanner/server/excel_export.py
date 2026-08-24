"""Build an .xlsx (Production Order layout) from Firestore orders.

One sheet, one row per line item, columns matching the final form. Header-level
fields are repeated on each line row.
"""
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

_thin = Border(left=Side("thin"), right=Side("thin"), top=Side("thin"), bottom=Side("thin"))
_hdr = Font(name="Arial", bold=True, size=11)
_nrm = Font(name="Arial", size=11)
_ctr = Alignment(horizontal="center", vertical="center", wrap_text=True)
_lft = Alignment(horizontal="left", vertical="center", wrap_text=True)
_rgt = Alignment(horizontal="right", vertical="center")
_hdr_fill = PatternFill("solid", fgColor="FFC000")

# (header text, order-line key, width, alignment, number-format)
COLUMNS = [
    ("Production Order", "order_no", 16, _ctr, None),
    ("วันที่", "document_date", 12, _ctr, None),
    ("Item No.", "series_no", 12, _ctr, None),
    ("ผลิตภัณฑ์", "product_name", 22, _lft, None),
    ("หน่วยผลิต", "plan_unit", 10, _ctr, None),
    ("ลำดับ", "row_no", 8, _ctr, "0"),
    ("รหัส", "item_no", 14, _ctr, None),
    ("รายการวัตถุดิบ", "item_description", 44, _lft, None),
    ("Type", "type", 11, _ctr, None),
    ("Issue Qty", "quantity", 12, _rgt, "#,##0.000"),
    ("คลังสินค้า", "whse", 11, _ctr, None),
    ("Plan", "plan", 12, _rgt, "#,##0.000"),
    ("หน่วย", "unit", 8, _ctr, None),
]
_HEADER_KEYS = {"order_no", "document_date", "series_no", "product_name",
                "plan_unit"}


def build_workbook(orders):
    wb = Workbook()
    ws = wb.active
    ws.title = "production_order"

    for c, (title, _, width, _, _) in enumerate(COLUMNS, 1):
        cell = ws.cell(row=1, column=c, value=title)
        cell.font = _hdr
        cell.fill = _hdr_fill
        cell.border = _thin
        cell.alignment = _ctr
        ws.column_dimensions[get_column_letter(c)].width = width

    r = 2
    for o in orders:
        for line in (o.get("lines") or [{}]):
            for c, (_, key, _, align, numfmt) in enumerate(COLUMNS, 1):
                val = o.get(key) if key in _HEADER_KEYS else line.get(key)
                cell = ws.cell(row=r, column=c, value=val)
                cell.font = _nrm
                cell.border = _thin
                cell.alignment = align
                if numfmt and isinstance(val, (int, float)):
                    cell.number_format = numfmt
            r += 1

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(r - 1, 1)}"

    _build_batch_sheet(wb, orders)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


BATCH_COLUMNS = [
    ("Production Order", 16, _ctr),
    ("วันที่เอกสาร", 12, _ctr),
    ("Item No.", 14, _ctr),
    ("ผลิตภัณฑ์", 22, _lft),
    ("MFG Date", 12, _ctr),
    ("EXP Date", 12, _ctr),
    ("Receive Qty.", 14, _rgt),
    ("หน่วย", 10, _ctr),
    ("คลัง", 12, _ctr),
]


def _build_batch_sheet(wb, orders):
    """Add a MFG_EXP worksheet — one row per production batch."""
    rows = []
    for o in orders:
        # The receiving warehouse sits on the product row of the form, not in
        # the MFG/EXP block.  Older forms leave that cell empty, and a warehouse
        # borrowed from the material lines would be the issuing one — wrong in a
        # way nobody could spot downstream.  Say "n/a" instead of guessing.
        whse = (o.get("product_whse") or "").strip() or "n/a"
        for b in o.get("batches") or []:
            rows.append((
                o.get("order_no"),
                o.get("document_date"),
                o.get("series_no"),
                o.get("product_name"),
                b.get("mfg_date"),
                b.get("exp_date"),
                b.get("batch_qty"),
                b.get("batch_unit"),
                (b.get("whse") or "").strip() or whse,
            ))
    if not rows:
        return

    ws = wb.create_sheet("MFG_EXP")
    for c, (title, width, _) in enumerate(BATCH_COLUMNS, 1):
        cell = ws.cell(row=1, column=c, value=title)
        cell.font = _hdr
        cell.fill = _hdr_fill
        cell.border = _thin
        cell.alignment = _ctr
        ws.column_dimensions[get_column_letter(c)].width = width

    for r_idx, row in enumerate(rows, 2):
        for c, (_, _, align) in enumerate(BATCH_COLUMNS, 1):
            cell = ws.cell(row=r_idx, column=c, value=row[c - 1])
            cell.font = _nrm
            cell.border = _thin
            cell.alignment = align
            if c == 7 and isinstance(row[c - 1], (int, float)):
                cell.number_format = "#,##0"

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(BATCH_COLUMNS))}{max(len(rows) + 1, 1)}"
