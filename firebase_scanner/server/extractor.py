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
- ยอดผลิต Plan -> plan_total (ตัวเลข เช่น 485.200 หรือ 23400)
  หาได้ 2 แบบ แล้วแต่ฟอร์ม:
  (ก) ช่องสีเหลืองเหนือตาราง
  (ข) อยู่ในหัวตารางฝั่งขวา ใต้หัวข้อ "ยอดผลิต (Plan)" ซึ่งอาจแบ่งเป็น "รอบที่ 1" / "รอบที่ 2" / "ยอดรวม"
      → ให้ใช้ค่าจากช่อง "ยอดรวม" ถ้ามี ถ้าไม่มีให้ใช้ "รอบที่ 1"
- ยอดผลิตจริง -> actual_total (ตัวเลข) — ช่องที่กำกับว่า "ยอดผลิต" ด้านล่างตาราง มักเขียนด้วยลายมือ
- หน่วยของยอดผลิต -> plan_unit (เช่น "KG", "ชิ้น")

ตาราง: แต่ละแถว = 1 รายการ (วัตถุดิบ/ทรัพยากร) -> lines[]
- ลำดับ             -> row_no (ตัวเลข)
- รหัส              -> item_no (เช่น "10202004", "P8-MC-E001", "DL-217")
- รายการวัตถุดิบ    -> item_description (ข้อความ ไทย/อังกฤษ)
- Type              -> type ("Item" หรือ "Resource")
- Qty               -> quantity_raw (ข้อความ) = ตัวเลขลายมือ "ตามที่เห็นทุกตัวอักษร"
  ⚠️ ห้ามแปลง ห้ามเติมหรือลบจุด/จุลภาคเอง — เห็น "12405" ให้ส่ง "12405", เห็น "12,405" ให้ส่ง "12,405"
  ถ้าว่างหรือเป็น "-" ให้ส่ง "0" (ระบบจะตีความตัวคั่นเองจากหน่วยและปริมาณตามแผน)
- คลังสินค้า (Whse) -> whse (เช่น "P8-PD05")
- ปริมาณที่ต้องใช้  -> plan_raw (ข้อความ) = ตัวเลขตามที่พิมพ์ในฟอร์มทุกตัวอักษร
  ⚠️ ห้ามแปลงหรือตัดจุด/จุลภาคเอง เห็น "23,400" ส่ง "23,400", เห็น "194.992" ส่ง "194.992"
  ⚠️ ถ้าใต้หัวข้อ "ยอดผลิต (Plan)" มี 2 คอลัมน์ย่อยคือ "Std ตามสูตร" กับ "ปริมาณที่ต้องใช้"
     ให้ใช้ค่าจาก "ปริมาณที่ต้องใช้" เท่านั้น ห้ามใช้ "Std ตามสูตร" (ซึ่งเป็นอัตราส่วนต่อหน่วย)
- หน่วย             -> unit (เช่น "KG", "Hour", "Hr", "ชิ้น", "ม้วน")

กติกา:
- อ่านทุกคอลัมน์ตั้งแต่ "ลำดับ" จนถึง "หน่วย" ให้ครบ — คอลัมน์ ปริมาณที่ต้องใช้ และ หน่วย
  มักอยู่ทางขวาของ Whse อย่าข้าม
- ข้ามเฉพาะคอลัมน์ที่อยู่ถัดจาก "หน่วย" ไปทางขวา (เช่น ยอดรวม) และข้อมูลหลังเส้นปะแนวตั้ง
- อ่านทุกแถวในตารางให้ครบ อย่าหยุดกลางคัน แม้บางแถวจะมีช่องว่างหรือขีด "-"
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
    {"row_no":1,"item_no":"10202004","item_description":"น้ำมันถั่วเหลือง ตรา MEI (Lamsoon)","type":"Item","quantity_raw":"3.819","whse":"P8-PD05","plan_raw":"2.961","unit":"KG"}
  ]
}
""".strip()


# --- auto-crop -------------------------------------------------------------
# Cropping at the dashed separator saves tokens, but cutting in the wrong place
# silently deletes columns the reader never learns were missing.  The rules
# below are deliberately conservative: off unless switched on, never cuts far
# into the page, and only accepts a line whose dashes are actually regular.
CROP_SEARCH_LEFT = 0.55
CROP_SEARCH_RIGHT = 0.995
CROP_MIN_KEEP = 0.85       # refuse to discard more than 15% of the width
CROP_DARK_THRESHOLD = 160
CROP_PADDING = 20
CROP_MIN_DASHES = 6
CROP_MAX_DASH_FRACTION = 0.06   # one dash is short next to the page height
CROP_MAX_CV = 0.45              # dashes and gaps must be evenly sized


def _cv(values):
    """Coefficient of variation — how irregular a set of run lengths is."""
    if len(values) < 2:
        return 999.0
    mean = sum(values) / len(values)
    if mean <= 0:
        return 999.0
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return (var ** 0.5) / mean


def _runs(flags):
    """Collapse a boolean column into (is_dark, length) runs."""
    out = []
    val, length = flags[0], 1
    for f in flags[1:]:
        if f == val:
            length += 1
        else:
            out.append((val, length))
            val, length = f, 1
    out.append((val, length))
    return out


def _find_dashed_line_x(img):
    """Return the x of a genuine vertical dashed rule, or None.

    A column of repeating table text alternates dark and light just as often as
    a dashed line does, which is why counting alternations alone picked the Qty
    column over the real separator.  A printed dash rule is also *regular*: the
    dashes are short, all about the same length, and evenly spaced.  That
    regularity is what this checks.
    """
    gray = img.convert("L")
    px = gray.load()
    w, h = gray.size
    x_start = int(w * CROP_SEARCH_LEFT)
    x_end = max(x_start + 1, int(w * CROP_SEARCH_RIGHT))
    max_dash = max(2, int(h * CROP_MAX_DASH_FRACTION))

    # Every column is examined: a printed rule can be a single pixel wide, and
    # sampling every few columns walks straight past it.  A cheap probe on one
    # row in eight rejects the blank majority before the full read.
    probe_step = max(1, h // 180)
    best_x, best_cv = None, CROP_MAX_CV
    for x in range(x_start, x_end):
        probe = [px[x, y] < CROP_DARK_THRESHOLD for y in range(0, h, probe_step)]
        probe_ratio = sum(probe) / len(probe)
        if probe_ratio < 0.08 or probe_ratio > 0.85:
            continue

        dark = [px[x, y] < CROP_DARK_THRESHOLD for y in range(h)]
        ratio = sum(dark) / h
        if ratio < 0.15 or ratio > 0.75:          # too sparse to be a rule, or a solid line
            continue

        runs = _runs(dark)
        dashes = [ln for is_dark, ln in runs if is_dark]
        gaps = [ln for is_dark, ln in runs[1:-1] if not is_dark]
        if len(dashes) < CROP_MIN_DASHES or len(gaps) < CROP_MIN_DASHES - 1:
            continue
        if max(dashes) > max_dash:                 # a long stroke means text or a solid rule
            continue

        irregularity = max(_cv(dashes), _cv(gaps))
        if irregularity < best_cv:
            best_cv, best_x = irregularity, x
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
        # Anything that would remove a real column is treated as a misdetection.
        if crop_x < w * CROP_MIN_KEEP:
            return media_type, raw
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


def images_from_upload(raw: bytes, content_type: str, filename: str = "",
                       auto_crop: bool = False):
    """Turn an uploaded file into a list of (media_type, bytes). PDFs → page images.

    Cropping at the dashed rule is opt-in.  It removes tokens from the bill,
    but a wrong cut removes data with no trace, so the default is to send the
    whole page and pay for it.
    """
    crop = _crop_at_dashed_line if auto_crop else (lambda b, m: (m, b))
    ct = (content_type or "").lower()
    name = (filename or "").lower()
    if ct == "application/pdf" or name.endswith(".pdf"):
        import fitz
        images = []
        doc = fitz.open(stream=raw, filetype="pdf")
        for page in doc:
            images.append(("image/png", page.get_pixmap(dpi=200).tobytes("png")))
        doc.close()
        return [crop(b, m) for m, b in images]
    if ct.startswith("image/"):
        m, b = _fix_exif(raw, ct)
        return [crop(b, m)]
    ext_media = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
    for ext, media in ext_media.items():
        if name.endswith("." + ext):
            m, b = _fix_exif(raw, media)
            return [crop(b, m)]
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


def _claude_text(prompt, key, model, max_tokens):
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(model=model, max_tokens=max_tokens,
                                 messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in msg.content if b.type == "text")


def _gemini_text(prompt, key, model, max_tokens):
    from google import genai
    client = genai.Client(api_key=key)
    return client.models.generate_content(model=model, contents=[prompt]).text


def _openai_text(prompt, key, model, max_tokens):
    from openai import OpenAI
    client = OpenAI(api_key=key)
    resp = client.chat.completions.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}])
    return resp.choices[0].message.content


_TEXT_PROVIDERS = {"claude": _claude_text, "gemini": _gemini_text, "openai": _openai_text}


def chat(prompt, provider, api_key, model, max_tokens=1024, retries=2):
    """Text-only completion, sharing the provider config used for extraction."""
    for attempt in range(retries + 1):
        try:
            return _TEXT_PROVIDERS[provider](prompt, api_key, model, max_tokens) or ""
        except Exception as e:  # noqa: BLE001
            if attempt < retries and any(k in str(e).lower() for k in _TRANSIENT):
                time.sleep(min(20, 2 * (2 ** attempt)))
                continue
            raise


def parse_json(text):
    """Public wrapper — model replies are JSON wrapped in stray prose or fences."""
    return _parse_json(text)


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


# --- number reading -------------------------------------------------------
# Handwritten "12405" comes back from the model as "12.405" or "12,405" with no
# way to tell a decimal point from a thousands separator by shape alone. The
# form carries the answer in the neighbouring cells: the unit says whether
# fractions are even possible, and the planned quantity says the order of
# magnitude. Guessing wrong turns 12,405 pieces into 12.4 and nobody sees it.

COUNT_UNITS = {
    "ชิ้น", "อัน", "ม้วน", "ใบ", "ถุง", "กล่อง", "แผ่น", "หลอด", "ขวด", "ซอง",
    "แพ็ค", "แพค", "pcs", "pc", "piece", "pieces", "ea", "roll", "sheet", "bag",
    "box", "pack",
}

# 12.405 · 1,234,567 — every group after a separator is exactly three digits
_GROUPED_RE = re.compile(r"^[-+]?\d{1,3}(?:[.,]\d{3})+$")


def _is_count_unit(unit):
    return str(unit or "").strip().lower() in COUNT_UNITS


def _split_reading(text):
    """Both readings of a grouped number, or None when it is not ambiguous."""
    text = str(text).strip()
    if not _GROUPED_RE.match(text):
        return None
    as_decimal = _num(text.replace(",", ""))
    as_thousands = _num(re.sub(r"[.,]", "", text))
    if as_decimal is None or as_thousands is None or as_decimal == as_thousands:
        return None
    return as_decimal, as_thousands


def _ratio_to(value, anchor):
    if not value or not anchor or value <= 0 or anchor <= 0:
        return None
    return max(value, anchor) / min(value, anchor)


def _resolve(text, anchor, unit, label):
    """Pick the reading of `text` that the surrounding numbers support.

    Returns (value, reinterpreted, reason).  The anchor — the planned amount
    for a quantity, the issued amount for a plan — decides, because a roll can
    genuinely be half used and only the neighbouring figure can tell a real
    fraction from a separator read as a decimal point.
    """
    if text is None or text == "":
        return None, False, ""
    readings = _split_reading(text)
    if readings is None:
        return _num(text), False, ""
    as_decimal, as_thousands = readings

    d_ratio = _ratio_to(as_decimal, anchor)
    t_ratio = _ratio_to(as_thousands, anchor)
    if d_ratio and t_ratio:
        if t_ratio < 10 <= d_ratio:
            return (as_thousands, True,
                    f"อ่าน {text} เป็น {as_thousands:,} เพราะใกล้{label} {anchor:,} มากกว่า")
        return as_decimal, False, ""

    # Nothing to compare against; the unit is the only evidence left.
    if _is_count_unit(unit):
        return (as_thousands, True,
                f"หน่วยเป็น “{unit}” นับเป็นจำนวนเต็ม จึงอ่าน {text} เป็น {as_thousands:,}")
    return as_decimal, False, ""


def read_quantity(raw, unit=None, plan=None):
    """Interpret a written quantity against the planned amount."""
    return _resolve(raw, _num(plan), unit, "ปริมาณตามแผน")


def read_plan(raw, unit=None, quantity=None):
    """Interpret a written planned amount against what was actually issued."""
    return _resolve(raw, _num(quantity), unit, "ปริมาณที่เบิกจริง")


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
        unit = _s(r.get("unit"))
        raw = r.get("quantity_raw")
        if raw in (None, ""):
            raw = r.get("quantity")
        plan_raw = r.get("plan_raw")
        if plan_raw in (None, ""):
            plan_raw = r.get("plan")

        # Resolve the quantity against the plan as written, then re-read the
        # plan against the settled quantity, so a separator misread on one side
        # cannot drag the other side with it.
        value, changed, reason = read_quantity(raw, unit, _num(plan_raw))
        plan, plan_changed, plan_reason = read_plan(plan_raw, unit, value)
        line = {
            "row_no": _num(r.get("row_no")) or i,
            "item_no": _s(r.get("item_no")),
            "item_description": _s(r.get("item_description")),
            "type": _s(r.get("type")) or "Item",
            "quantity": value if value is not None else 0,
            "quantity_raw": _s(raw),
            "whse": _s(r.get("whse")),
            "plan": plan,
            "unit": unit,
        }
        line["plan_raw"] = _s(plan_raw)
        if changed:
            line["qty_reinterpreted"] = True
            line["qty_note"] = reason
        if plan_changed:
            line["plan_reinterpreted"] = True
            line["plan_note"] = plan_reason
        out["lines"].append(line)
    return out
