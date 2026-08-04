"""Unit tests for excel_export.py — build_workbook is a pure function."""
import io
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from openpyxl import load_workbook
from excel_export import build_workbook, COLUMNS


def _load(orders):
    raw = build_workbook(orders)
    assert isinstance(raw, bytes)
    assert len(raw) > 0
    return load_workbook(io.BytesIO(raw))


class TestBuildWorkbook:
    def test_empty_orders(self):
        wb = _load([])
        ws = wb.active
        assert ws.title == "production_order"
        # header row only
        assert ws.cell(1, 1).value == "Production Order"
        assert ws.cell(2, 1).value is None

    def test_header_count(self):
        wb = _load([])
        ws = wb.active
        headers = [ws.cell(1, c).value for c in range(1, len(COLUMNS) + 1)]
        expected = [col[0] for col in COLUMNS]
        assert headers == expected

    def test_header_style(self):
        wb = _load([])
        ws = wb.active
        cell = ws.cell(1, 1)
        assert cell.font.bold is True
        assert cell.fill.fgColor.rgb == "00FFC000"

    def test_single_order_no_lines(self):
        orders = [{"order_no": "OD001", "lines": []}]
        wb = _load(orders)
        ws = wb.active
        # order with no lines still produces 1 data row (with empty line dict)
        assert ws.cell(2, 1).value == "OD001"

    def test_single_order_with_lines(self):
        orders = [{
            "order_no": "OD002",
            "document_date": "2026-07-01",
            "series_no": "7010101004",
            "product_name": "ขนมปัง",
            "plan_total": 100.0,
            "actual_total": 98.5,
            "plan_unit": "KG",
            "lines": [
                {"row_no": 1, "item_no": "A001", "item_description": "แป้ง",
                 "type": "Item", "quantity": 50.0, "whse": "WH1", "plan": 55.0, "unit": "KG"},
                {"row_no": 2, "item_no": "A002", "item_description": "น้ำตาล",
                 "type": "Item", "quantity": 20.0, "whse": "WH1", "plan": 22.0, "unit": "KG"},
            ],
        }]
        wb = _load(orders)
        ws = wb.active
        # 2 lines → 2 data rows
        assert ws.cell(2, 1).value == "OD002"  # row 2, col 1 = order_no
        assert ws.cell(3, 1).value == "OD002"  # repeated on each line
        assert ws.cell(2, 8).value == 1  # row_no of first line
        assert ws.cell(3, 8).value == 2  # row_no of second line
        assert ws.cell(2, 9).value == "A001"  # item_no
        assert ws.cell(3, 9).value == "A002"

    def test_header_fields_repeated_per_line(self):
        orders = [{
            "order_no": "OD003",
            "product_name": "สินค้า",
            "lines": [{"row_no": 1}, {"row_no": 2}],
        }]
        wb = _load(orders)
        ws = wb.active
        assert ws.cell(2, 1).value == "OD003"
        assert ws.cell(3, 1).value == "OD003"
        assert ws.cell(2, 4).value == "สินค้า"
        assert ws.cell(3, 4).value == "สินค้า"

    def test_multiple_orders(self):
        orders = [
            {"order_no": "A", "lines": [{"row_no": 1}]},
            {"order_no": "B", "lines": [{"row_no": 1}, {"row_no": 2}]},
        ]
        wb = _load(orders)
        ws = wb.active
        assert ws.cell(2, 1).value == "A"
        assert ws.cell(3, 1).value == "B"
        assert ws.cell(4, 1).value == "B"

    def test_number_format_applied(self):
        orders = [{"order_no": "X", "plan_total": 100.5, "lines": [{"quantity": 3.14}]}]
        wb = _load(orders)
        ws = wb.active
        # plan_total column (5th) should have number format
        assert ws.cell(2, 5).number_format == "#,##0.000"
        # quantity column (12th) should have number format
        assert ws.cell(2, 12).number_format == "#,##0.000"

    def test_string_values_no_number_format(self):
        orders = [{"order_no": "X", "plan_total": "N/A", "lines": [{}]}]
        wb = _load(orders)
        ws = wb.active
        # string value should not get number format
        assert ws.cell(2, 5).value == "N/A"

    def test_freeze_panes(self):
        wb = _load([{"order_no": "X", "lines": [{}]}])
        ws = wb.active
        assert ws.freeze_panes == "A2"

    def test_auto_filter(self):
        wb = _load([{"order_no": "X", "lines": [{}]}])
        ws = wb.active
        assert ws.auto_filter.ref is not None
        assert ws.auto_filter.ref.startswith("A1:")
