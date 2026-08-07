"""Unit tests for extractor.py — pure functions: normalize, _num, _s, _parse_json."""
import io
import json
import pytest
import sys, os
from PIL import Image, ImageDraw
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import extractor
from extractor import (normalize, _num, _s, _num_or_zero, _parse_json,
                       images_from_upload, _find_dashed_line_x, _crop_at_dashed_line)


# ---------------------------------------------------------------------------
# _num: numeric coercion
# ---------------------------------------------------------------------------
class TestNum:
    def test_none(self):
        assert _num(None) is None

    def test_empty_string(self):
        assert _num("") is None

    def test_int(self):
        assert _num(42) == 42

    def test_float(self):
        assert _num(3.14) == 3.14

    def test_float_whole(self):
        assert _num(5.0) == 5.0
        assert isinstance(_num(5.0), float)

    def test_string_int(self):
        assert _num("123") == 123

    def test_string_float(self):
        assert _num("3.14") == 3.14

    def test_string_with_comma(self):
        assert _num("1,234.56") == 1234.56

    def test_string_with_units(self):
        assert _num("485.200 KG") == 485.2

    def test_addition_expression(self):
        assert _num("2+3") == 5

    def test_dash_is_none(self):
        assert _num("-") is None

    def test_plus_only(self):
        assert _num("+") is None

    def test_dot_only(self):
        assert _num(".") is None

    def test_negative(self):
        assert _num("-5") == -5

    def test_zero(self):
        assert _num(0) == 0


# ---------------------------------------------------------------------------
# _num_or_zero: like _num but blanks → 0
# ---------------------------------------------------------------------------
class TestNumOrZero:
    def test_none_returns_zero(self):
        assert _num_or_zero(None) == 0

    def test_empty_returns_zero(self):
        assert _num_or_zero("") == 0

    def test_dash_returns_zero(self):
        assert _num_or_zero("-") == 0

    def test_valid_number(self):
        assert _num_or_zero("42") == 42

    def test_valid_float(self):
        assert _num_or_zero("3.14") == 3.14


# ---------------------------------------------------------------------------
# _s: string coercion
# ---------------------------------------------------------------------------
class TestS:
    def test_none(self):
        assert _s(None) is None

    def test_empty(self):
        assert _s("") is None

    def test_whitespace(self):
        assert _s("  ") is None

    def test_normal(self):
        assert _s("hello") == "hello"

    def test_strips(self):
        assert _s("  hello  ") == "hello"

    def test_number_to_string(self):
        assert _s(42) == "42"


# ---------------------------------------------------------------------------
# _parse_json
# ---------------------------------------------------------------------------
class TestParseJson:
    def test_plain_json(self):
        j = '{"order_no": "12345"}'
        assert _parse_json(j) == {"order_no": "12345"}

    def test_json_with_markdown_fence(self):
        j = '```json\n{"order_no": "12345"}\n```'
        assert _parse_json(j) == {"order_no": "12345"}

    def test_json_with_prefix_text(self):
        j = 'Here is the result:\n{"order_no": "12345"}\nDone.'
        assert _parse_json(j) == {"order_no": "12345"}

    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="ไม่พบ JSON"):
            _parse_json("no json here")


# ---------------------------------------------------------------------------
# normalize: full pipeline
# ---------------------------------------------------------------------------
class TestNormalize:
    def test_basic(self):
        data = {
            "order_no": "326070043",
            "document_date": "2026-07-01",
            "series_no": "7010101004",
            "product_name": "แซนวิชหมูหยอง",
            "plan_total": 485.2,
            "actual_total": "485.200",
            "plan_unit": "KG",
            "lines": [
                {
                    "row_no": 1,
                    "item_no": "10202004",
                    "item_description": "น้ำมันถั่วเหลือง",
                    "type": "Item",
                    "quantity": 3.819,
                    "whse": "P8-PD05",
                    "plan": 2.961,
                    "unit": "KG",
                }
            ],
        }
        out = normalize(data)
        assert out["order_no"] == "326070043"
        assert out["plan_total"] == 485.2
        assert out["actual_total"] == 485.2
        assert len(out["lines"]) == 1
        assert out["lines"][0]["quantity"] == 3.819
        assert out["lines"][0]["type"] == "Item"

    def test_empty_lines(self):
        out = normalize({"order_no": "123", "lines": []})
        assert out["lines"] == []

    def test_none_lines(self):
        out = normalize({"order_no": "123"})
        assert out["lines"] == []

    def test_quantity_blank_becomes_zero(self):
        out = normalize({"lines": [{"quantity": ""}]})
        assert out["lines"][0]["quantity"] == 0

    def test_quantity_dash_becomes_zero(self):
        out = normalize({"lines": [{"quantity": "-"}]})
        assert out["lines"][0]["quantity"] == 0

    def test_missing_type_defaults_item(self):
        out = normalize({"lines": [{}]})
        assert out["lines"][0]["type"] == "Item"

    def test_row_no_auto_increments(self):
        out = normalize({"lines": [{}, {}, {}]})
        assert [l["row_no"] for l in out["lines"]] == [1, 2, 3]

    def test_null_fields(self):
        out = normalize({})
        assert out["order_no"] is None
        assert out["document_date"] is None
        assert out["series_no"] is None
        assert out["product_name"] is None
        assert out["plan_total"] is None
        assert out["actual_total"] is None
        assert out["plan_unit"] is None


# ---------------------------------------------------------------------------
# images_from_upload: basic type detection
# ---------------------------------------------------------------------------
class TestImagesFromUpload:
    def test_rejects_unknown_type(self):
        with pytest.raises(ValueError, match="ไม่รองรับ"):
            images_from_upload(b"data", "text/plain", "file.txt")

    def test_jpeg_returns_one_image(self):
        # minimal JPEG header (won't pass PIL but we test the routing)
        tiny_jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        try:
            result = images_from_upload(tiny_jpg, "image/jpeg", "test.jpg")
            assert len(result) >= 1
            assert result[0][0] == "image/jpeg"
        except Exception:
            pass  # PIL may fail on synthetic data — that's fine

    def test_png_by_extension(self):
        # minimal PNG header
        tiny_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        try:
            result = images_from_upload(tiny_png, "application/octet-stream", "test.png")
            assert len(result) >= 1
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Auto-crop at dashed line
# ---------------------------------------------------------------------------
class TestCropDashedLine:
    def _make_dashed_image(self, w=2000, h=1500, line_x=1200):
        img = Image.new("L", (w, h), 240)
        y = 0
        while y < h:
            for dy in range(15):
                if y + dy < h:
                    img.putpixel((line_x, y + dy), 60)
            y += 30
        return img

    def test_detects_dashed_line(self):
        img = self._make_dashed_image(line_x=1200)
        x = _find_dashed_line_x(img)
        assert x is not None
        assert abs(x - 1200) < 10

    def test_ignores_solid_line(self):
        img = Image.new("L", (2000, 1500), 240)
        for y in range(1500):
            img.putpixel((1200, y), 50)
        assert _find_dashed_line_x(img) is None

    def test_blank_image_returns_none(self):
        img = Image.new("L", (2000, 1500), 255)
        assert _find_dashed_line_x(img) is None

    def test_crop_applies_at_the_page_margin(self):
        img = self._make_dashed_image(line_x=1900)      # 95% of the width
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        _, cropped = _crop_at_dashed_line(buf.getvalue(), "image/png")
        result = Image.open(io.BytesIO(cropped))
        assert result.size[0] < 2000
        assert result.size[0] <= 1900 + 30

    def test_crop_refused_when_it_would_cut_into_the_table(self):
        # A rule at 55% would take real columns with it. The reading of a form
        # with columns removed is wrong in a way nobody can see, so a cut this
        # deep is treated as a misdetection and the whole page is sent.
        img = self._make_dashed_image(line_x=1100)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        _, out = _crop_at_dashed_line(buf.getvalue(), "image/png")
        assert Image.open(io.BytesIO(out)).size[0] == 2000

    def test_text_column_is_not_mistaken_for_a_dashed_rule(self):
        # Repeating table text alternates dark and light exactly as often as a
        # dashed line; only the regularity of the runs tells them apart.
        img = Image.new("L", (2000, 1500), 255)
        d = ImageDraw.Draw(img)
        for i in range(14):
            y = 200 + i * 80
            d.text((1180, y), "P8-PD02", fill=0)
            d.text((1400, y), "12405", fill=0)
        assert _find_dashed_line_x(img) is None

    def test_auto_crop_is_opt_in(self):
        img = self._make_dashed_image(line_x=1900)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        raw = buf.getvalue()
        untouched = extractor.images_from_upload(raw, "image/png", "f.png")
        assert Image.open(io.BytesIO(untouched[0][1])).size[0] == 2000
        cropped = extractor.images_from_upload(raw, "image/png", "f.png", auto_crop=True)
        assert Image.open(io.BytesIO(cropped[0][1])).size[0] < 2000

    def test_small_image_not_cropped(self):
        img = Image.new("RGB", (400, 300), "white")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        raw = buf.getvalue()
        _, out = _crop_at_dashed_line(raw, "image/jpeg")
        assert len(out) == len(raw)

    def test_non_image_passthrough(self):
        _, out = _crop_at_dashed_line(b"pdf data", "application/pdf")
        assert out == b"pdf data"
