"""Vision extraction — reads a ใบเบิกวัตถุดิบ form image/PDF and returns structured JSON.

Supports three providers, chosen at runtime via config: claude | gemini | openai.
SDKs are imported lazily so the app runs with only the provider you actually use.
"""
import base64
import json
import os
import re

# ---------------------------------------------------------------------------
# Prompt: describes the form layout so the model knows which cell maps to what.
# ---------------------------------------------------------------------------
EXTRACTION_PROMPT = r"""
คุณเป็นระบบอ่านเอกสาร "ใบเบิกวัตถุดิบ" ของโรงงาน NSL Foods (ฟอร์ม FM-PD-01/02)
เอกสารเป็นตารางเขียนด้วยลายมือภาษาไทย โปรดอ่านค่าจากรูปแล้วส่งกลับเป็น JSON เท่านั้น

ตำแหน่งข้อมูลในฟอร์ม:
- มุมขวาบน: เลขที่ใบสั่งผลิต เขียนรูปแบบ "เลขชุด/เลขที่ออก" เช่น 32510/1215
  -> ให้รวมเป็น production_order_no = "32510-1215" (คั่นด้วยเครื่องหมาย -)
- มุมขวาบน "วันที่": วันที่เอกสาร เช่น 23/10/25 -> document_date รูปแบบ ISO "2025-10-23"
- ตารางกลาง: แต่ละแถวคือวัตถุดิบ 1 รายการ
  * คอลัมน์ "รหัส" -> material_code (ตัวเลข เช่น 30102036) ถ้าไม่มีให้ null
  * คอลัมน์ "รายการวัตถุดิบ / สินค้า" -> material_name (ข้อความไทย)
  * ลำดับแถว (1,2,3,...) -> item_no
  * คอลัมน์ "ยอดผลิต (Actual) / ปริมาณที่ต้องจริง" (ตัวเลขเขียนมือช่องกลาง) -> actual_qty
    ถ้าช่องว่างให้ null ; ถ้าเป็นทศนิยมให้คงไว้ เช่น 433.193
  * หน่วย (แผ่น, kg, ม้วน, ใบ) -> unit
- ล่างสุดขวา: "จำนวนพนักงาน ... คน" -> worker_count ; "ทำงาน ... ชม." -> work_hours
- กล่องล่าง (ยอดผลิต pack): แต่ละบรรทัดมี MFG (วันที่ผลิต), EXP (วันหมดอายุ), จำนวน pack
  -> packs[] โดย pack_no ไล่ 1,2,3 ; mfg_date/exp_date เป็น ISO ; quantity เป็นจำนวนเต็ม
    ถ้าเขียนแบบ "12233+2" ให้บวกได้ผลลัพธ์ = 12235

กติกา:
- ตอบเป็น JSON ล้วนเท่านั้น ห้ามมีข้อความอื่นหรือ markdown fence
- ค่าที่อ่านไม่ได้/ว่าง ให้เป็น null
- ปี พ.ศ./ค.ศ.: ปีเขียนมือ 2 หลักเช่น 25 หมายถึง ค.ศ. 2025 (บวก 2000)
- ตัวเลขส่งเป็น number ไม่ใส่ comma

รูปแบบ JSON ที่ต้องส่งกลับ:
{
  "production_order_no": "32510-1215",
  "document_date": "2025-10-23",
  "materials": [
    {"item_no": 1, "material_code": "30102036", "material_name": "ขนมปังคัสตาร์ด 1.5 cm", "actual_qty": 69496, "unit": "แผ่น"}
  ],
  "workforce": {"worker_count": 17, "work_hours": 216},
  "packs": [
    {"pack_no": 1, "mfg_date": "2025-10-24", "exp_date": "2025-11-22", "quantity": 12235, "unit": "pack"}
  ]
}
""".strip()

MEDIA_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def load_images(path):
    """Return a list of (media_type, raw_bytes). PDFs are rendered page-by-page."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        try:
            import fitz  # PyMuPDF
        except ImportError as e:
            raise RuntimeError("ต้องติดตั้ง pymupdf เพื่ออ่านไฟล์ PDF") from e
        images = []
        doc = fitz.open(path)
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            images.append(("image/png", pix.tobytes("png")))
        doc.close()
        if not images:
            raise RuntimeError("PDF ไม่มีหน้า")
        return images
    media = MEDIA_BY_EXT.get(ext)
    if not media:
        raise RuntimeError(f"ไม่รองรับนามสกุลไฟล์: {ext}")
    with open(path, "rb") as f:
        return [(media, f.read())]


def _parse_json(text):
    """Pull the JSON object out of a model response that may include fences/prose."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"ไม่พบ JSON ในคำตอบของโมเดล: {text[:200]}")
    return json.loads(text[start:end + 1])


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
def _extract_claude(images, api_key, model):
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    content = []
    for media, raw in images:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media,
                       "data": base64.b64encode(raw).decode()},
        })
    content.append({"type": "text", "text": EXTRACTION_PROMPT})
    msg = client.messages.create(
        model=model, max_tokens=4096,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(b.text for b in msg.content if b.type == "text")


def _extract_gemini(images, api_key, model):
    # Prefer the current google-genai SDK; fall back to the legacy one.
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        parts = [EXTRACTION_PROMPT]
        for media, raw in images:
            parts.append(types.Part.from_bytes(data=raw, mime_type=media))
        resp = client.models.generate_content(model=model, contents=parts)
        return resp.text
    except ImportError:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model_obj = genai.GenerativeModel(model)
        parts = [EXTRACTION_PROMPT]
        for media, raw in images:
            parts.append({"mime_type": media, "data": raw})
        resp = model_obj.generate_content(parts)
        return resp.text


def _extract_openai(images, api_key, model):
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    content = [{"type": "text", "text": EXTRACTION_PROMPT}]
    for media, raw in images:
        b64 = base64.b64encode(raw).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:{media};base64,{b64}"}})
    resp = client.chat.completions.create(
        model=model, max_tokens=4096,
        messages=[{"role": "user", "content": content}],
    )
    return resp.choices[0].message.content


_PROVIDERS = {
    "claude": _extract_claude,
    "gemini": _extract_gemini,
    "openai": _extract_openai,
}


def extract(path, provider, api_key, model):
    """Extract structured data from one file. Returns the parsed+normalized dict."""
    if provider not in _PROVIDERS:
        raise ValueError(f"ไม่รู้จัก provider: {provider}")
    if not api_key:
        raise ValueError(f"ยังไม่ได้ตั้งค่า API key ของ {provider}")
    images = load_images(path)
    raw_text = _PROVIDERS[provider](images, api_key, model)
    data = _parse_json(raw_text)
    return normalize(data)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v).replace(",", "").strip()
    if "+" in s:  # handwriting like "12233+2"
        try:
            return sum(float(x) for x in s.split("+"))
        except ValueError:
            pass
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return None


def normalize(data):
    out = {
        "production_order_no": (str(data.get("production_order_no")).strip()
                                 if data.get("production_order_no") else None),
        "document_date": data.get("document_date") or None,
        "materials": [],
        "workforce": {"worker_count": None, "work_hours": None},
        "packs": [],
    }
    for i, m in enumerate(data.get("materials") or [], start=1):
        out["materials"].append({
            "item_no": _num(m.get("item_no")) or i,
            "material_code": (str(m["material_code"]).strip()
                              if m.get("material_code") else None),
            "material_name": (m.get("material_name") or "").strip() or None,
            "actual_qty": _num(m.get("actual_qty")),
            "unit": (m.get("unit") or "").strip() or None,
        })
    wf = data.get("workforce") or {}
    out["workforce"]["worker_count"] = _num(wf.get("worker_count"))
    out["workforce"]["work_hours"] = _num(wf.get("work_hours"))
    for i, p in enumerate(data.get("packs") or [], start=1):
        out["packs"].append({
            "pack_no": _num(p.get("pack_no")) or i,
            "mfg_date": p.get("mfg_date") or None,
            "exp_date": p.get("exp_date") or None,
            "quantity": _num(p.get("quantity")),
            "unit": (p.get("unit") or "pack").strip(),
        })
    return out
