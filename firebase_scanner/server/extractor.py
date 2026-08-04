"""Vision extraction for the cloud backend — works on in-memory bytes (uploaded files).

Same form-reading logic as the local app, but the input is raw bytes from an HTTP
upload rather than a filesystem path. Providers: claude | gemini | openai.
Auto-rotates images (EXIF + AI orientation detection) before extraction.
"""
import base64
import io
import json
import re
import time

from PIL import Image, ImageOps

EXTRACTION_PROMPT = r"""
คุณเป็นระบบอ่านเอกสาร "ใบเบิกวัตถุดิบ / Production Order" ของโรงงาน (ตารางภาษาไทย+อังกฤษ)
โปรดอ่านค่าจากรูปแล้วส่งกลับเป็น JSON เท่านั้น

ข้อมูลส่วนหัว:
- Production Order   -> order_no (เช่น "326070043") — Primary Key
- วันที่              -> document_date (แปลงเป็น YYYY-MM-DD เช่น "2026-07-01")
- Series No. / รหัส   -> series_no — อ่านจากรหัสตัวเลข 7–10 หลัก (เช่น "7010101004", "7010401001") ที่อยู่ในคอลัมน์ "รหัส" ของแถวแรกของตาราง (แถวเดียวกับชื่อผลิตภัณฑ์) ⚠️ ห้ามใช้เลข Production Order
- ชื่อผลิตภัณฑ์        -> product_name — อ่านจากแถวแรกของตาราง (แถวเดียวกับ series_no) ในคอลัมน์ "สินค้า/รายการวัตถุดิบ" ซึ่งเป็นชื่อสินค้าหลัก เช่น "แซนวิชหมูหยองน้ำพริกเผา", "แซนวิชเดนิชคาโบว์นาร่า" — ⚠️ ห้ามใช้ชื่อแผนกหรือหัวเรื่องเอกสาร
- ยอดผลิต Plan (ช่องสีเหลือง ด้านบนตาราง) -> plan_total (ตัวเลข เช่น 485.200)
- ยอดผลิตจริง (ด้านล่างตาราง "ยอดผลิต")     -> actual_total (ตัวเลข เช่น 485.2)
- หน่วยของยอดผลิต                          -> plan_unit (เช่น "KG")

ตาราง: แต่ละแถว = 1 รายการ (วัตถุดิบ/ทรัพยากร) -> lines[]
- ลำดับ             -> row_no (ตัวเลข)
- รหัส              -> item_no (เช่น "10202004", "P8-MC-E001", "DL-217")
- รายการวัตถุดิบ    -> item_description (ข้อความ ไทย/อังกฤษ)
- Type              -> type ("Item" หรือ "Resource")
- Qty               -> quantity (ตัวเลข ที่เขียนด้วยมือ — ⚠️ ถ้าว่างหรือ "-" ให้ใส่ 0)
- คลังสินค้า (Whse) -> whse (เช่น "P8-PD05")
- ยอดผลิต (Plan)    -> plan (ตัวเลข ทศนิยม — ปริมาณที่ต้องใช้ตามแผน)
- หน่วย             -> unit (เช่น "KG", "Hour", "Hr")

กติกา:
- อ่านเฉพาะคอลัมน์ "ลำดับ" ถึง "หน่วย" เท่านั้น (ถ้ามีเส้นปะแบ่ง ให้อ่านเฉพาะฝั่งซ้ายของเส้นปะ) — ⚠️ ข้อมูลหลังคอลัมน์ "หน่วย" หรือหลังเส้นปะ ให้ข้ามทั้งหมด
- ตอบ JSON ล้วน ไม่มี markdown ; ช่องว่าง/อ่านไม่ได้ = null (ยกเว้น quantity ที่ว่างหรือ "-" = 0) ; ตัวเลขไม่ใส่ comma และไม่ใส่หน่วย

รูปแบบ JSON:
{
  "order_no": "326070043",
  "document_date": "2026-07-01",
  "series_no": "7010101004",
  "product_name": "แซนวิชหมูหยองน้ำพริกเผา",
  "plan_total": 485.200,
  "actual_total": 485.2,
  "plan_unit": "KG",
  "lines": [
    {"row_no":1,"item_no":"10202004","item_description":"น้ำมันถั่วเหลือง ตรา MEI (Lamsoon)","type":"Item","quantity":3.819,"whse":"P8-PD05","plan":2.961,"unit":"KG"}
  ]
}
""".strip()


CROP_SEARCH_LEFT = 0.45
CROP_SEARCH_RIGHT = 0.85
CROP_DARK_THRESHOLD = 160
CROP_PADDING = 20


def _find_dashed_line_x(img):
    """Detect the vertical dashed line and return its x-coordinate, or None."""
    gray = img.convert("L")
    w, h = gray.size
    x_start = int(w * CROP_SEARCH_LEFT)
    x_end = int(w * CROP_SEARCH_RIGHT)
    best_x, best_score = None, 0
    for x in range(x_start, x_end, max(1, w // 400)):
        col = [gray.getpixel((x, y)) for y in range(h)]
        dark = [v < CROP_DARK_THRESHOLD for v in col]
        n = len(dark)
        dark_count = sum(dark)
        dark_ratio = dark_count / n if n else 0
        if dark_ratio < 0.05 or dark_ratio > 0.6:
            continue
        segments = []
        run_val = dark[0]
        run_len = 1
        for i in range(1, n):
            if dark[i] == run_val:
                run_len += 1
            else:
                segments.append((run_val, run_len))
                run_val = dark[i]
                run_len = 1
        segments.append((run_val, run_len))
        dark_segs = [ln for is_dark, ln in segments if is_dark]
        gap_segs = [ln for is_dark, ln in segments if not is_dark]
        if len(dark_segs) < 4 or len(gap_segs) < 3:
            continue
        score = len(dark_segs) * len(gap_segs)
        if score > best_score:
            best_score = score
            best_x = x
    return best_x


def _crop_at_dashed_line(raw: bytes, media_type: str) -> tuple[str, bytes]:
    """Crop image at the vertical dashed line, keeping only the left portion."""
    if media_type not in ("image/jpeg", "image/png", "image/webp"):
        return media_type, raw
    try:
        img = Image.open(io.BytesIO(raw))
        w, h = img.size
        if w < 800 or h < 400:
            return media_type, raw
        x = _find_dashed_line_x(img)
        if x is None:
            return media_type, raw
        crop_x = min(x + CROP_PADDING, w)
        cropped = img.crop((0, 0, crop_x, h))
        buf = io.BytesIO()
        fmt = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}.get(media_type, "PNG")
        cropped.save(buf, format=fmt)
        return media_type, buf.getvalue()
    except Exception:  # noqa: BLE001
        return media_type, raw


def _fix_exif(raw: bytes, media_type: str) -> tuple[str, bytes]:
    """Apply EXIF orientation tag and strip it so the pixels are upright."""
    if media_type not in ("image/jpeg", "image/png", "image/webp"):
        return media_type, raw
    try:
        img = Image.open(io.BytesIO(raw))
        oriented = ImageOps.exif_transpose(img)
        if oriented is img:
            return media_type, raw
        buf = io.BytesIO()
        fmt = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}.get(media_type, "PNG")
        oriented.save(buf, format=fmt)
        return media_type, buf.getvalue()
    except Exception:  # noqa: BLE001
        return media_type, raw


def _rotate_image(raw: bytes, media_type: str, degrees: int) -> tuple[str, bytes]:
    """Rotate image by given degrees (90, 180, 270) counter-clockwise."""
    if degrees == 0:
        return media_type, raw
    try:
        img = Image.open(io.BytesIO(raw))
        rotated = img.rotate(degrees, expand=True)
        buf = io.BytesIO()
        fmt = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}.get(media_type, "PNG")
        rotated.save(buf, format=fmt)
        return media_type, buf.getvalue()
    except Exception:  # noqa: BLE001
        return media_type, raw


ORIENTATION_PROMPT = (
    "You are checking the orientation of a scanned Thai factory document. "
    "Look at the text in the document — especially the header row and printed text. "
    "Determine which way the text is facing:\n"
    "- If text reads normally (left-to-right, top-to-bottom) → answer 0\n"
    "- If the page is upside-down (text is inverted/flipped 180°) → answer 180\n"
    "- If text reads bottom-to-top (page rotated 90° clockwise) → answer 90\n"
    "- If text reads top-to-bottom (page rotated 90° counter-clockwise) → answer 270\n\n"
    "IMPORTANT: Look at the Thai/English text direction carefully. "
    "If you can read 'Production Order' or 'ใบเบิก' normally, it's 0. "
    "If those words appear upside-down, it's 180.\n"
    "Answer ONLY a single number: 0, 90, 180, or 270"
)


def images_from_upload(raw: bytes, content_type: str, filename: str = ""):
    """Turn an uploaded file into a list of (media_type, bytes). PDFs → page images.

    Automatically crops at the vertical dashed line (if detected) to keep
    only the left portion with the essential data columns.
    """
    ct = (content_type or "").lower()
    name = (filename or "").lower()
    if ct == "application/pdf" or name.endswith(".pdf"):
        import fitz
        images = []
        doc = fitz.open(stream=raw, filetype="pdf")
        for page in doc:
            images.append(("image/png", page.get_pixmap(dpi=200).tobytes("png")))
        doc.close()
        return [_crop_at_dashed_line(m, b) for m, b in images]
    if ct.startswith("image/"):
        fixed = _fix_exif(raw, ct)
        return [_crop_at_dashed_line(*fixed)]
    ext_media = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
    for ext, media in ext_media.items():
        if name.endswith("." + ext):
            fixed = _fix_exif(raw, media)
            return [_crop_at_dashed_line(*fixed)]
    raise ValueError(f"ไม่รองรับไฟล์ประเภทนี้: {content_type or filename}")


def _parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1:
        raise ValueError(f"ไม่พบ JSON ในคำตอบโมเดล: {text[:200]}")
    return json.loads(text[s:e + 1])


def _claude(images, key, model):
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    content = [{"type": "image", "source": {"type": "base64", "media_type": m,
               "data": base64.b64encode(b).decode()}} for m, b in images]
    content.append({"type": "text", "text": EXTRACTION_PROMPT})
    msg = client.messages.create(model=model, max_tokens=4096,
                                 messages=[{"role": "user", "content": content}])
    return "".join(b.text for b in msg.content if b.type == "text")


def _gemini(images, key, model):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=key)
    parts = [EXTRACTION_PROMPT] + [types.Part.from_bytes(data=b, mime_type=m) for m, b in images]
    return client.models.generate_content(model=model, contents=parts).text


def _openai(images, key, model):
    from openai import OpenAI
    client = OpenAI(api_key=key)
    content = [{"type": "text", "text": EXTRACTION_PROMPT}]
    for m, b in images:
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:{m};base64,{base64.b64encode(b).decode()}"}})
    resp = client.chat.completions.create(model=model, max_tokens=4096,
                                          messages=[{"role": "user", "content": content}])
    return resp.choices[0].message.content


_PROVIDERS = {"claude": _claude, "gemini": _gemini, "openai": _openai}


_TRANSIENT = ("429", "rate limit", "rate_limit", "quota", "resource_exhausted",
              "resourceexhausted", "500", "502", "503", "overloaded",
              "unavailable", "deadline", "timeout", "temporarily")


def _call_with_retry(provider, images, api_key, model, retries=4):
    """Retry on rate-limit / transient errors so large batches don't fail mid-run."""
    for attempt in range(retries + 1):
        try:
            return _PROVIDERS[provider](images, api_key, model)
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if attempt < retries and any(k in msg for k in _TRANSIENT):
                time.sleep(min(30, 2 * (2 ** attempt)))  # 2,4,8,16,30s backoff
                continue
            raise


def _detect_orientation_once(images, provider, api_key, model):
    """Single orientation detection call."""
    if provider == "claude":
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        content = [{"type": "image", "source": {"type": "base64", "media_type": m,
                    "data": base64.b64encode(b).decode()}} for m, b in images[:1]]
        content.append({"type": "text", "text": ORIENTATION_PROMPT})
        msg = client.messages.create(model=model, max_tokens=16,
                                     messages=[{"role": "user", "content": content}])
        txt = "".join(b.text for b in msg.content if b.type == "text")
    elif provider == "gemini":
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        parts = [ORIENTATION_PROMPT] + [types.Part.from_bytes(data=b, mime_type=m)
                                        for m, b in images[:1]]
        txt = client.models.generate_content(model=model, contents=parts).text
    else:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        m0, b0 = images[0]
        content = [{"type": "text", "text": ORIENTATION_PROMPT},
                   {"type": "image_url",
                    "image_url": {"url": f"data:{m0};base64,{base64.b64encode(b0).decode()}"}}]
        resp = client.chat.completions.create(model=model, max_tokens=16,
                                              messages=[{"role": "user", "content": content}])
        txt = resp.choices[0].message.content
    nums = re.findall(r"\d+", txt.strip())
    deg = int(nums[0]) if nums else 0
    return deg if deg in (0, 90, 180, 270) else 0


def _detect_orientation(images, provider, api_key, model):
    """Ask the AI how many degrees the document is rotated (2-vote majority)."""
    try:
        votes = []
        for _ in range(2):
            votes.append(_detect_orientation_once(images, provider, api_key, model))
        if votes[0] == votes[1]:
            return votes[0]
        votes.append(_detect_orientation_once(images, provider, api_key, model))
        from collections import Counter
        most = Counter(votes).most_common(1)[0][0]
        return most
    except Exception:  # noqa: BLE001
        return 0


def extract(images, provider, api_key, model):
    if provider not in _PROVIDERS:
        raise ValueError(f"ไม่รู้จัก provider: {provider}")
    if not api_key:
        raise ValueError(f"ยังไม่ได้ตั้งค่า API key ของ {provider}")

    raw_text = _call_with_retry(provider, images, api_key, model)
    data = _parse_json(raw_text)

    if not data.get("order_no") and not data.get("lines"):
        deg = _detect_orientation(images, provider, api_key, model)
        if deg:
            rotated = [_rotate_image(b, m, deg) for m, b in images]
            raw_text = _call_with_retry(provider, rotated, api_key, model)
            data = _parse_json(raw_text)

    return normalize(data)


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v
    s = re.sub(r"[^\d.\-+]", "", str(v).replace(",", ""))  # strip THB/units/spaces
    if not s or s in ("+", "-", "."):
        return None
    if "+" in s[1:]:
        try:
            return sum(float(x) for x in s.split("+") if x)
        except ValueError:
            pass
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return None


def _s(v):
    v = ("" if v is None else str(v)).strip()
    return v or None


def _num_or_zero(v):
    """Like _num but returns 0 instead of None for blanks / dashes."""
    n = _num(v)
    return n if n is not None else 0


def normalize(data):
    out = {
        "order_no": _s(data.get("order_no")),
        "document_date": _s(data.get("document_date")),
        "series_no": _s(data.get("series_no")),
        "product_name": _s(data.get("product_name")),
        "plan_total": _num(data.get("plan_total")),
        "actual_total": _num(data.get("actual_total")),
        "plan_unit": _s(data.get("plan_unit")),
        "lines": [],
    }
    for i, r in enumerate(data.get("lines") or [], 1):
        out["lines"].append({
            "row_no": _num(r.get("row_no")) or i,
            "item_no": _s(r.get("item_no")),
            "item_description": _s(r.get("item_description")),
            "type": _s(r.get("type")) or "Item",
            "quantity": _num_or_zero(r.get("quantity")),
            "whse": _s(r.get("whse")),
            "plan": _num(r.get("plan")),
            "unit": _s(r.get("unit")),
        })
    return out
