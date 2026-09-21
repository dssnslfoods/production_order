"""Cloud Run backend (FastAPI) for the ใบเบิกวัตถุดิบ scanner.

Flow: mobile web uploads a photo → /api/scan extracts with a vision model →
saves the image to Cloud Storage and the structured record to Firestore →
/api/export builds an .xlsx from Firestore on demand.
"""
import concurrent.futures
import io
import logging
import os
import threading
import traceback
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from PIL import Image
from pydantic import BaseModel

import analytics
import ask_ai
import auth
from auth import require_role
import excel_export
import extractor
import firestore_store as store

log = logging.getLogger(__name__)

app = FastAPI(title="ใบเบิกวัตถุดิบ Cloud Scanner")

IMAGE_MAX_PIXELS = 2048
IMAGE_QUALITY = 85


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": True, "code": exc.status_code, "detail": exc.detail},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request: Request, exc: Exception):
    log.error("Unhandled error: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": True, "code": 500, "detail": "เกิดข้อผิดพลาดภายในระบบ"},
    )

admin_only = require_role("super_admin", "admin")
admin_or_approver = require_role("super_admin", "admin", "approver")
any_role = require_role("super_admin", "admin", "approver", "reviewer", "staff")
super_only = require_role("super_admin")


def _fid(user):
    """Effective factory_id for data queries. super_admin sees all (None)."""
    if user["role"] == "super_admin":
        return None
    return user.get("factory_id")


def _require_factory(user):
    """Raise 403 if a non-super_admin user has no factory assigned."""
    if user["role"] == "super_admin":
        return
    if not user.get("factory_id"):
        raise HTTPException(status_code=403,
                            detail="ยังไม่ได้สังกัดโรงงาน กรุณาติดต่อ Admin เพื่อกำหนดโรงงาน")

_ENV_KEY = {"claude": "CLAUDE_API_KEY", "gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY"}


_ERROR_MAP = [
    ("ไม่รองรับไฟล์ประเภทนี้", "ไฟล์ประเภทนี้ไม่รองรับ — กรุณาใช้ไฟล์ JPG, PNG หรือ PDF"),
    ("ไม่พบ JSON", "AI อ่านเอกสารไม่สำเร็จ — ลองถ่ายรูปใหม่ให้ชัดขึ้น"),
    ("api key", "ยังไม่ได้ตั้งค่า API Key — กรุณาตั้งค่าในหน้า Settings"),
    ("api_key", "ยังไม่ได้ตั้งค่า API Key — กรุณาตั้งค่าในหน้า Settings"),
    ("rate limit", "AI ถูกเรียกถี่เกินไป — กรุณารอสักครู่แล้วลองใหม่"),
    ("rate_limit", "AI ถูกเรียกถี่เกินไป — กรุณารอสักครู่แล้วลองใหม่"),
    ("quota", "โควต้า AI หมด — ตรวจสอบยอดใช้งานกับผู้ให้บริการ"),
    ("resource_exhausted", "โควต้า AI หมด — ตรวจสอบยอดใช้งานกับผู้ให้บริการ"),
    ("timeout", "การเชื่อมต่อ AI หมดเวลา — กรุณาลองใหม่"),
    ("connection", "เชื่อมต่อ AI ไม่ได้ — ตรวจสอบอินเทอร์เน็ต"),
    ("401", "API Key ไม่ถูกต้องหรือหมดอายุ — กรุณาตรวจสอบใน Settings"),
    ("403", "ไม่มีสิทธิ์เรียก AI — ตรวจสอบ API Key"),
    ("invalid_api_key", "API Key ไม่ถูกต้อง — กรุณาตรวจสอบใน Settings"),
    ("could not process", "AI ประมวลผลรูปไม่ได้ — ลองถ่ายใหม่ให้ชัดขึ้นหรือใช้ไฟล์ขนาดเล็กลง"),
    ("image", "ไฟล์ภาพเสียหายหรืออ่านไม่ได้ — ลองถ่ายรูปใหม่"),
    ("mime_type", "ไฟล์ภาพเสียหายหรือรูปแบบไม่ถูกต้อง — ลองถ่ายรูปใหม่"),
    ("overloaded", "ระบบ AI มีภาระงานสูง — กรุณารอสักครู่แล้วลองใหม่"),
    ("500", "ระบบ AI ขัดข้อง — กรุณาลองใหม่ภายหลัง"),
    ("503", "ระบบ AI ไม่พร้อมให้บริการชั่วคราว — กรุณาลองใหม่"),
]


def _friendly_error(exc):
    """Convert a raw Python exception into a user-readable Thai message."""
    msg = str(exc).lower()
    for keyword, friendly in _ERROR_MAP:
        if keyword.lower() in msg:
            return friendly
    return f"เกิดข้อผิดพลาด: {str(exc)[:150]}"


def _api_key(provider):
    """Prefer an admin-entered key (Firestore); fall back to the deploy-time env var."""
    keys = store.get_settings().get("api_keys") or {}
    return keys.get(provider) or os.environ.get(_ENV_KEY.get(provider, ""), "")


def _masked_config(user_email=None):
    s = store.get_settings()
    keys = s.get("api_keys") or {}

    def masked(p):
        k = keys.get(p)
        if k:
            return "••••" + k[-4:]
        if os.environ.get(_ENV_KEY[p]):
            return "ตั้งจากเซิร์ฟเวอร์"
        return ""

    import drive_puller
    return {
        "provider": s["provider"],
        "models": s["models"],
        "keys_configured": {p: bool(_api_key(p)) for p in _ENV_KEY},
        "keys_masked": {p: masked(p) for p in _ENV_KEY},
        "drive_folder_id": s.get("drive_folder_id", ""),
        "auto_crop": bool(s.get("auto_crop")),
        "drive_sa_email": drive_puller.sa_email(),
        "user": user_email,
    }


def _prepare_images(raw, content_type, filename):
    """Build the page images for the model, honouring the auto-crop setting.

    Returns (images, ai_bytes) where ai_bytes is the first page as the model
    will see it — kept only when cropping actually changed the picture, so a
    misread can be checked against the real input rather than guessed at.
    """
    auto_crop = bool(store.get_settings().get("auto_crop"))
    images = extractor.images_from_upload(raw, content_type, filename, auto_crop=auto_crop)
    ai_bytes = None
    if auto_crop and images and images[0][1] != raw:
        ai_bytes = images[0]
    return images, ai_bytes


def _optimize_image(raw: bytes, content_type: str) -> tuple[bytes, str]:
    """Resize large images and compress to JPEG to reduce Storage and AI costs."""
    ct = (content_type or "").lower()
    if not ct.startswith("image/") or ct == "image/gif":
        return raw, content_type
    try:
        img = Image.open(io.BytesIO(raw))
        w, h = img.size
        if max(w, h) > IMAGE_MAX_PIXELS:
            ratio = IMAGE_MAX_PIXELS / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=IMAGE_QUALITY, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001
        return raw, content_type


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/config")
def get_config(user=Depends(auth.verify_token)):
    cfg = _masked_config(user["email"])
    cfg["role"] = user["role"]
    cfg["factory_id"] = user.get("factory_id")
    cfg["factory_code"] = user.get("factory_code")
    cfg["factory_name"] = user.get("factory_name")
    perms = store.get_permissions()
    cfg["permissions"] = perms.get(user["role"], [])
    cfg["all_permissions"] = perms
    if user["role"] == "super_admin":
        cfg["factories"] = store.list_factories()
    return cfg


class ConfigIn(BaseModel):
    provider: Optional[str] = None
    models: Optional[dict] = None
    api_keys: Optional[dict] = None
    drive_folder_id: Optional[str] = None
    auto_crop: Optional[bool] = None


@app.post("/api/config")
def set_config(body: ConfigIn, user=Depends(admin_only)):
    patch = body.model_dump(exclude_none=True)
    # Never store a masked placeholder as a real key.
    if "api_keys" in patch:
        patch["api_keys"] = {k: v for k, v in patch["api_keys"].items()
                             if v and not str(v).startswith("••••")}
    if "drive_folder_id" in patch:
        import drive_puller
        patch["drive_folder_id"] = drive_puller.parse_folder_id(patch["drive_folder_id"])
    store.save_settings(patch)
    return _masked_config(user["email"])


def _page_label(data):
    return f"หน้า {data.get('page_no') or 1}/{data.get('page_total') or 1}"


def _absorb_page(dup, data, storage_path=None):
    """Fold a repeat of an existing Order No. into it as a continuation page.

    A multi-page form repeats its Order No. on every sheet, so treating the
    second sheet as a duplicate would silently drop half the requisition.
    Returns (status, detail); raises when the page cannot be filed at all, so
    the file stays visible in the failure queue instead of disappearing.
    """
    outcome = store.merge_order_page(dup["id"], data, storage_path=storage_path)
    order_no = data.get("order_no") or "-"
    if outcome == "merged":
        return "success", f"Order {order_no} · รวม{_page_label(data)} ({len(data.get('lines') or [])} รายการ)"
    if outcome == "locked":
        raise RuntimeError(
            f"Order {order_no} อนุมัติ/ส่งออกไปแล้ว จึงเพิ่ม{_page_label(data)} ไม่ได้ "
            f"— กรุณาตรวจสอบว่าเป็นเอกสารหน้าใหม่จริงหรือไม่")
    if outcome == "missing":
        raise RuntimeError(f"ไม่พบ Order {order_no} ที่จะรวมหน้า — ลองสแกนใหม่อีกครั้ง")
    return "skipped", f"Order No. {order_no} {_page_label(data)} มีในระบบแล้ว — ข้าม"


_ORDER_LOCKS = {}
_ORDER_LOCKS_GUARD = threading.Lock()


def _order_lock(order_no):
    """One lock per Order No., so parallel workers cannot both file the same set."""
    with _ORDER_LOCKS_GUARD:
        return _ORDER_LOCKS.setdefault(order_no or "", threading.Lock())


def _file_multipage(data, p, provider, pfid, order_no, total):
    """Park a sheet of a multi-page form, and file the order once the set is whole."""
    dup = store.find_by_order_no(order_no, factory_id=pfid)
    if dup:
        status, detail = _absorb_page(dup, data, storage_path=p.get("storage_path"))
        store.delete_pending(p["id"])
        return status, detail

    store.hold_page(p["id"], data)
    held = store.list_held_pages(order_no, factory_id=pfid)
    have = sorted({int(h.get("held_page_no") or 1) for h in held})
    missing = [n for n in range(1, total + 1) if n not in have]
    if missing:
        return "waiting", (f"Order {order_no or '-'} · ได้หน้า {'/'.join(map(str, have))} "
                           f"— พักไว้ในคิว รอหน้า {'/'.join(map(str, missing))}")

    assembled, images = store.assemble_pages(held)
    store.add_order(assembled, None, provider,
                    user_email=p.get("uploaded_by"),
                    source_filename=p.get("filename"),
                    factory_id=pfid, page_images=images)
    for h in held:
        store.delete_pending(h["id"])
    return "success", (f"Order {order_no or '-'} · รวมครบ {total} หน้า · "
                       f"{len(assembled.get('lines') or [])} รายการ")


def _file_queued_scan(data, p, provider, pfid):
    """Decide what a freshly-read queue file becomes. -> (status, detail)

    An incomplete multi-page form must not reach the orders list: half a
    requisition looks complete enough to approve and send to SAP.  Such a sheet
    is parked on its queue entry, reading and all, and the set is filed as one
    order only once every page has arrived.
    """
    order_no = data.get("order_no")
    total = int(data.get("page_total") or 1)
    if total > 1:
        # Sibling sheets of one form usually land in the same cron batch and are
        # read in parallel; without this, both could see the set as complete and
        # file the order twice.
        with _order_lock(order_no):
            return _file_multipage(data, p, provider, pfid, order_no, total)

    dup = store.find_by_order_no(order_no, factory_id=pfid)
    if dup:
        status, detail = _absorb_page(dup, data, storage_path=p.get("storage_path"))
        store.delete_pending(p["id"])
        return status, detail

    store.add_order(data, p["storage_path"], provider,
                    user_email=p.get("uploaded_by"), source_filename=p.get("filename"),
                    factory_id=pfid)
    store.delete_pending(p["id"])
    return "success", f"Order {order_no or '-'} · {len(data.get('lines') or [])} รายการ"


@app.post("/api/scan")
async def scan(file: UploadFile = File(...), user=Depends(auth.verify_token)):
    _require_factory(user)
    settings = store.get_settings()
    provider = settings["provider"]
    model = settings["models"].get(provider, "")
    key = _api_key(provider)
    if not key:
        raise HTTPException(status_code=400, detail=f"ยังไม่ได้ตั้งค่า API key ของ {provider} (ตั้งใน Secret/env)")

    raw = await file.read()
    optimized, opt_ct = _optimize_image(raw, file.content_type)
    try:
        images, ai_bytes = _prepare_images(optimized, opt_ct, file.filename)
        data = extractor.extract(images, provider, key, model)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"อ่านฟอร์มไม่สำเร็จ: {e}")

    fid = _fid(user)
    dup = store.find_by_order_no(data.get("order_no"), factory_id=fid)
    if dup:
        try:
            status, detail = _absorb_page(dup, data)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        if status == "skipped":
            raise HTTPException(status_code=409, detail=detail)
        analytics.invalidate_cache()
        store.log_activity("scan", user["email"], user["role"], detail, dup["id"],
                           factory_id=fid)
        return {"id": dup["id"], "data": data, "merged": True, "detail": detail,
                "summary": {"lines": len(data.get("lines", []))}}

    blob_path = store.upload_image(optimized, opt_ct, file.filename)
    ai_path = (store.upload_image(ai_bytes[1], ai_bytes[0], "ai_" + (file.filename or "page"))
               if ai_bytes else None)
    order_id = store.add_order(data, blob_path, provider, user_email=user["email"],
                               ai_image=ai_path, factory_id=fid)
    analytics.invalidate_cache()
    store.log_activity("scan", user["email"], user["role"],
                       f"สแกนไฟล์ {file.filename} → Order {data.get('order_no') or '-'}", order_id,
                       factory_id=fid)
    return {"id": order_id, "data": data,
            "summary": {"lines": len(data.get("lines", []))}}


def _process_queue(trigger="manual", factory_id=None):
    """Scan every pending/failed file → Firestore. Shared by manual button and scheduler."""
    settings = store.get_settings()
    provider = settings["provider"]
    model = settings["models"].get(provider, "")
    key = _api_key(provider)
    if not key:
        return {"error": f"ยังไม่ได้ตั้งค่า API key ของ {provider}", "processed": 0}
    result = {"processed": 0, "succeeded": 0, "failed": 0, "dead": 0, "items": []}
    for p in store.list_pending(factory_id=factory_id):
        if p.get("status") not in ("pending", "failed"):
            continue
        pfid = p.get("factory_id") or factory_id
        item = {"file": p.get("filename"), "status": "", "detail": ""}
        try:
            raw = store.download_bytes(p["storage_path"])
            images, _ai = _prepare_images(raw, p.get("content_type") or "", p.get("filename") or "")
            data = extractor.extract(images, provider, key, model)
            status, detail = _file_queued_scan(data, p, provider, pfid)
            item["status"] = status
            item["detail"] = detail
            if status == "success":
                result["succeeded"] += 1
            elif status == "waiting":
                result["waiting"] = result.get("waiting", 0) + 1
            else:
                result["skipped"] = result.get("skipped", 0) + 1
        except Exception as e:  # noqa: BLE001
            outcome = store.fail_pending(p["id"], _friendly_error(e))
            item["status"] = outcome
            item["detail"] = _friendly_error(e)
            if outcome == "dead":
                result["dead"] += 1
            else:
                result["failed"] += 1
        result["processed"] += 1
        result["items"].append(item)
    if result["processed"]:
        try:
            store.log_run(trigger, result, factory_id=factory_id)
        except Exception:  # noqa: BLE001
            pass
    return result


def _auto_orient(raw: bytes, content_type: str) -> tuple[str, bytes]:
    """EXIF fix + AI orientation detection → store image upright."""
    ct, fixed = extractor._fix_exif(raw, content_type or "")
    try:
        settings = store.get_settings()
        provider = settings["provider"]
        model = settings["models"].get(provider, "")
        key = _api_key(provider)
        if not key:
            return ct, fixed
        images = [(ct, fixed)]
        deg = extractor._detect_orientation(images, provider, key, model)
        if deg:
            ct, fixed = extractor._rotate_image(fixed, ct, deg)
    except Exception:  # noqa: BLE001
        pass
    return ct, fixed


@app.post("/api/queue")
async def queue(files: List[UploadFile] = File(...), user=Depends(auth.verify_token)):
    _require_factory(user)
    fid = _fid(user)
    saved = []
    for f in files:
        raw = await f.read()
        optimized, opt_ct = _optimize_image(raw, f.content_type or "")
        store.add_pending(optimized, opt_ct, f.filename, user["email"],
                          factory_id=fid)
        saved.append(f.filename)
    store.log_activity("upload_queue", user["email"], user["role"],
                       f"อัปโหลด {len(saved)} ไฟล์เข้าคิว", factory_id=fid)
    return {"queued": saved, "count": len(saved)}


@app.get("/api/pending")
def pending(user=Depends(auth.verify_token)):
    items = store.list_pending(factory_id=_fid(user))
    if user["role"] == "staff":
        items = [p for p in items if p.get("uploaded_by") == user["email"]]
    return {"pending": items}


@app.get("/api/pending/{pid}/preview")
def pending_preview(pid: str, user=Depends(auth.verify_token)):
    p = store.get_pending(pid)
    if not p:
        raise HTTPException(status_code=404, detail="ไม่พบไฟล์ในคิว")
    if user["role"] == "staff" and p.get("uploaded_by") != user["email"]:
        raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์ดูรายการนี้")
    try:
        raw = store.download_bytes(p["storage_path"])
        ct = p.get("content_type") or "image/jpeg"
        return Response(content=raw, media_type=ct)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"ดู preview ไม่ได้: {e}")


@app.delete("/api/pending/{pid}")
def pending_delete(pid: str, user=Depends(auth.verify_token)):
    p = store.get_pending(pid)
    if not p:
        raise HTTPException(status_code=404, detail="ไม่พบไฟล์ในคิว")
    if user["role"] == "staff" and p.get("uploaded_by") != user["email"]:
        raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์ลบรายการนี้")
    if p.get("storage_path"):
        try:
            store.bucket().blob(p["storage_path"]).delete()
        except Exception:  # noqa: BLE001
            pass
    store.delete_pending(pid)
    return {"deleted": pid}


def _pull_drive():
    """Pull new files from the configured Drive folder into the queue (best-effort)."""
    fid = store.get_settings().get("drive_folder_id")
    if not fid:
        return {"pulled": 0}
    try:
        import drive_puller
        return drive_puller.pull(fid)
    except Exception as e:  # noqa: BLE001
        return {"pulled": 0, "error": str(e)[:200]}


@app.post("/api/drive/pull")
def drive_pull(user=Depends(admin_only)):
    fid = store.get_settings().get("drive_folder_id")
    if not fid:
        raise HTTPException(status_code=400, detail="ยังไม่ได้ตั้งค่าโฟลเดอร์ Google Drive")
    import drive_puller
    try:
        return drive_puller.pull(fid)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"ดึงจาก Drive ไม่สำเร็จ: {e}")


@app.post("/api/process")
def process(user=Depends(auth.verify_token)):
    pulled = _pull_drive()
    fid = _fid(user)
    r = _process_queue(factory_id=fid)
    r["drive_pulled"] = pulled.get("pulled", 0)
    if pulled.get("error"):
        r["drive_error"] = pulled["error"]
    return r


@app.post("/api/process/{pid}")
def process_one(pid: str, user=Depends(auth.verify_token)):
    """Process a single pending file by ID."""
    p = store.get_pending(pid)
    if not p:
        raise HTTPException(status_code=404, detail="ไม่พบไฟล์ในคิว")
    # "failed" is a file waiting on another attempt, not a finished one — picking
    # it from the queue is how a person retries it without waiting for the cron.
    if p.get("status") == "held":
        return {"status": "waiting",
                "detail": f"Order {p.get('held_order_no') or '-'} หน้า "
                          f"{p.get('held_page_no')}/{p.get('held_page_total')} "
                          f"สแกนแล้ว — รอหน้าที่เหลือ ไม่ต้องสแกนซ้ำ"}
    if p.get("status") not in ("pending", "failed"):
        return {"status": "skipped", "detail": "ไฟล์นี้ถูกประมวลผลแล้ว"}
    settings = store.get_settings()
    provider = settings["provider"]
    model = settings["models"].get(provider, "")
    key = _api_key(provider)
    if not key:
        raise HTTPException(status_code=400, detail=f"ยังไม่ได้ตั้งค่า API key ของ {provider}")
    try:
        raw = store.download_bytes(p["storage_path"])
        images, _ai = _prepare_images(raw, p.get("content_type") or "", p.get("filename") or "")
        data = extractor.extract(images, provider, key, model)
        pfid = p.get("factory_id") or _fid(user)
        status, detail = _file_queued_scan(data, p, provider, pfid)
        if status == "success":
            store.log_activity("scan", user["email"], user["role"],
                               f"สแกน {p.get('filename')} → {detail}", factory_id=pfid)
        return {"status": status, "detail": detail}
    except Exception as e:  # noqa: BLE001
        store.fail_pending(pid, _friendly_error(e))
        return {"status": "failed", "detail": _friendly_error(e)}


CRON_BATCH_SIZE = int(os.environ.get("CRON_BATCH_SIZE", "10"))
CRON_WORKERS = int(os.environ.get("CRON_WORKERS", "5"))


def _process_one_pending(p, provider, key, model):
    """Process a single pending item. Thread-safe — used by parallel cron."""
    pfid = p.get("factory_id")
    item = {"file": p.get("filename"), "status": "", "detail": ""}
    try:
        raw = store.download_bytes(p["storage_path"])
        images, _ai = _prepare_images(raw, p.get("content_type") or "", p.get("filename") or "")
        data = extractor.extract(images, provider, key, model)
        status, detail = _file_queued_scan(data, p, provider, pfid)
        item["status"] = status
        item["detail"] = detail
        return item, status
    except Exception as e:  # noqa: BLE001
        outcome = store.fail_pending(p["id"], _friendly_error(e))
        item["status"] = outcome
        item["detail"] = _friendly_error(e)
        return item, outcome


@app.post("/api/cron/process")
def cron_process(x_cron_key: str = Header(default="")):
    secret = os.environ.get("CRON_SECRET", "")
    if not secret or x_cron_key != secret:
        raise HTTPException(status_code=403, detail="invalid cron key")
    pulled = _pull_drive()
    settings = store.get_settings()
    provider = settings["provider"]
    model = settings["models"].get(provider, "")
    key = _api_key(provider)
    if not key:
        return {"error": f"ยังไม่ได้ตั้งค่า API key ของ {provider}", "processed": 0,
                "drive_pulled": pulled.get("pulled", 0)}
    pending = [p for p in store.list_pending() if p.get("status") in ("pending", "failed")]
    batch = pending[:CRON_BATCH_SIZE]
    result = {"processed": 0, "succeeded": 0, "failed": 0, "dead": 0,
              "skipped": 0, "items": [], "queue_remaining": max(0, len(pending) - len(batch))}
    if batch:
        with concurrent.futures.ThreadPoolExecutor(max_workers=CRON_WORKERS) as pool:
            futures = {pool.submit(_process_one_pending, p, provider, key, model): p for p in batch}
            for f in concurrent.futures.as_completed(futures):
                item, outcome = f.result()
                result["items"].append(item)
                result["processed"] += 1
                if outcome == "success":
                    result["succeeded"] += 1
                elif outcome == "skipped":
                    result["skipped"] += 1
                elif outcome == "waiting":
                    result["waiting"] = result.get("waiting", 0) + 1
                elif outcome == "dead":
                    result["dead"] += 1
                else:
                    result["failed"] += 1
        try:
            store.log_run("schedule", result)
        except Exception:  # noqa: BLE001
            pass
    result["drive_pulled"] = pulled.get("pulled", 0)
    try:
        result["logs_cleaned"] = store.cleanup_old_logs()
    except Exception:  # noqa: BLE001
        pass
    return result


@app.get("/api/schedule")
def get_schedule(user=Depends(auth.verify_token)):
    import scheduler_admin
    try:
        return scheduler_admin.get_schedule()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"อ่านตารางเวลาไม่ได้: {e}")


class ScheduleIn(BaseModel):
    cron: Optional[str] = None
    timezone: Optional[str] = "Asia/Bangkok"
    action: Optional[str] = None


@app.post("/api/schedule")
def set_schedule(body: ScheduleIn, user=Depends(admin_only)):
    import scheduler_admin
    try:
        if body.action == "pause":
            return scheduler_admin.pause_job()
        if body.action == "resume":
            return scheduler_admin.resume_job()
        if not body.cron:
            raise HTTPException(status_code=400, detail="ต้องระบุ cron หรือ action")
        return scheduler_admin.update_schedule(body.cron, body.timezone or "Asia/Bangkok")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"ตั้งตารางเวลาไม่สำเร็จ: {e}")


@app.get("/api/report")
def report(user=Depends(auth.verify_token)):
    import datetime
    from collections import Counter
    fid = _fid(user)
    orders, _ = store.list_orders(limit=2000, factory_id=fid)
    today = datetime.datetime.utcnow().date().isoformat()
    today_count = sum(1 for o in orders if (o.get("scanned_at") or "").startswith(today))
    runs = store.list_runs(limit=20, factory_id=fid)
    total_skipped = sum(r.get("skipped", 0) for r in runs)

    upload_counter = Counter()
    for o in orders:
        by = o.get("scanned_by") or "ระบบ"
        upload_counter[by] += 1
    uploaders = [{"email": k, "count": v} for k, v in upload_counter.most_common(20)]

    approvals = store.list_activity(limit=50, factory_id=fid)
    approval_logs = [a for a in approvals if a.get("action") in ("approve", "bulk_approve")]

    approved = [o for o in orders if o.get("status") == "approved"]
    variance = analytics.plan_variance(approved)
    status_counts = Counter(o.get("status") or "unknown" for o in orders)

    return {
        "total_scanned": store.count_orders(factory_id=fid),
        "today_scanned": today_count,
        "total_skipped": total_skipped,
        "runs": runs,
        "uploaders": uploaders,
        "approval_logs": approval_logs[:20],
        "variance": variance,
        "status_counts": dict(status_counts),
    }


# Someone who can neither edit drafts nor review (an approver, by default) only
# deals with what a reviewer has passed on, so earlier stages are hidden from them.
APPROVER_STATUSES = ("pending_approval", "returned_to_review", "approved", "exported")


def _visible_statuses(user):
    if _has_perm(user, "edit_order") or _has_perm(user, "confirm_review"):
        return None
    return APPROVER_STATUSES


def _visible_order(order_id, user):
    o = _factory_order(order_id, user)
    shown = _visible_statuses(user)
    if shown and o.get("status") not in shown:
        raise HTTPException(status_code=404, detail="ไม่พบรายการ")
    return o


@app.get("/api/orders")
def orders(limit: int = 100, cursor: Optional[str] = None,
           user=Depends(auth.verify_token)):
    items, next_cursor = store.list_orders(limit=min(limit, 500), cursor=cursor,
                                           factory_id=_fid(user),
                                           statuses=_visible_statuses(user))
    return {"orders": items, "next_cursor": next_cursor}


@app.get("/api/orders/{order_id}")
def order_detail(order_id: str, user=Depends(auth.verify_token)):
    o = _visible_order(order_id, user)
    imgs = store.page_images_of(o)
    o["image_pages"] = [i.get("page") for i in imgs]
    o["has_image"] = bool(imgs)
    return o


@app.get("/api/orders/{order_id}/image")
def order_image(order_id: str, variant: str = "source", page: int = 0,
                user=Depends(auth.verify_token)):
    """variant=ai returns the page as the model saw it, when cropping altered it.

    `page` picks one sheet of a multi-page form; without it the first is served.
    """
    o = _visible_order(order_id, user)
    if variant == "ai":
        path = o.get("ai_image")
    else:
        imgs = store.page_images_of(o)
        path = next((i["path"] for i in imgs if i.get("page") == page), None) if page \
            else (imgs[0]["path"] if imgs else None)
    if not path:
        raise HTTPException(status_code=404, detail="ไม่พบรูปภาพ")
    try:
        raw = store.download_bytes(path)
        return Response(content=raw, media_type="image/jpeg")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"โหลดรูปไม่ได้: {e}")


def _require_perm(perm):
    """Dynamic permission check from Firestore settings."""
    async def _check(authorization: str = Header(default="")):
        user = await auth.verify_token(authorization)
        perms = store.get_permissions().get(user["role"], [])
        if perm not in perms:
            raise HTTPException(status_code=403,
                                detail=f"ไม่มีสิทธิ์ '{perm}' (role: {user['role']})")
        return user
    return _check


def _factory_order(order_id, user):
    """Fetch an order the caller's factory may act on (404 otherwise, like the list)."""
    o = store.get_order(order_id)
    fid = _fid(user)
    if not o or (fid and o.get("factory_id") != fid):
        raise HTTPException(status_code=404, detail="ไม่พบรายการ")
    return o


def _has_perm(user, perm):
    return perm in store.get_permissions().get(user["role"], [])


def _log_stage(action, verb, o, order_id, user, extra=""):
    analytics.invalidate_cache()
    store.log_activity(action, user["email"], user["role"],
                       f"{verb} Order {o.get('order_no') or '-'}{extra}", order_id,
                       factory_id=o.get("factory_id") or _fid(user))


# Workflow: draft (staff checks the scan) -> pending_review (reviewer)
#   -> pending_approval (approver) -> approved.
# The approver may send pending_approval back as returned_to_review.
REVIEW_STATUSES = ("pending_review", "returned_to_review")


@app.post("/api/orders/{order_id}/submit-review")
def order_submit_review(order_id: str, user=Depends(_require_perm("edit_order"))):
    o = _factory_order(order_id, user)
    if o.get("status") != "draft":
        raise HTTPException(status_code=400,
                            detail="ส่งตรวจสอบได้เฉพาะรายการฉบับร่างที่ยังไม่ได้ส่ง")
    result = store.submit_for_review(order_id, user["email"])
    _log_stage("submit_review", "ส่งตรวจสอบ", o, order_id, user)
    return result


@app.post("/api/orders/{order_id}/confirm-review")
def order_confirm_review(order_id: str, user=Depends(_require_perm("confirm_review"))):
    o = _factory_order(order_id, user)
    # A reviewer who opens a draft has checked it themselves; routing it through
    # their own pending_review queue first would just be an extra click.
    if o.get("status") not in ("draft",) + REVIEW_STATUSES:
        raise HTTPException(status_code=400,
                            detail="ส่งต่อไปอนุมัติได้เฉพาะฉบับร่าง รายการที่รอตรวจสอบ หรือถูกตีกลับ")
    result = store.confirm_review(order_id, user["email"])
    _log_stage("confirm_review", "ตรวจสอบแล้ว ส่งต่อไปอนุมัติ", o, order_id, user,
               " (จากฉบับร่าง)" if o.get("status") == "draft" else "")
    return result


@app.post("/api/orders/{order_id}/approve")
def order_approve(order_id: str, user=Depends(_require_perm("approve"))):
    o = _factory_order(order_id, user)
    if o.get("status") != "pending_approval":
        raise HTTPException(status_code=400,
                            detail="อนุมัติได้เฉพาะรายการที่ผ่านการตรวจสอบแล้ว (รออนุมัติ)")
    result = store.approve_order(order_id, user["email"])
    _log_stage("approve", "อนุมัติ", o, order_id, user)
    return result


class ReturnToReviewIn(BaseModel):
    reason: str


@app.post("/api/orders/{order_id}/return-to-review")
def order_return_to_review(order_id: str, body: ReturnToReviewIn,
                           user=Depends(_require_perm("return_to_review"))):
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="ต้องระบุเหตุผลในการตีกลับ")
    o = _factory_order(order_id, user)
    if o.get("status") != "pending_approval":
        raise HTTPException(status_code=400,
                            detail="ตีกลับได้เฉพาะรายการที่อยู่ในสถานะรออนุมัติ")
    result = store.return_order_to_review(order_id, user["email"], reason)
    _log_stage("return_to_review", "ตีกลับ", o, order_id, user, f": {reason}")
    return result


class OrderIn(BaseModel):
    order_no: Optional[str] = None
    document_date: Optional[str] = None
    series_no: Optional[str] = None
    product_name: Optional[str] = None
    product_whse: Optional[str] = None
    plan_total: Optional[float] = None
    actual_total: Optional[float] = None
    plan_unit: Optional[str] = None
    lines: Optional[list] = None
    batches: Optional[list] = None


@app.put("/api/orders/{order_id}")
def order_update(order_id: str, body: OrderIn, user=Depends(auth.verify_token)):
    """Only whoever owns the current stage may edit: staff on a draft, the
    reviewer while it is in review. Past that the record is frozen."""
    o = _factory_order(order_id, user)
    status = o.get("status")
    if status == "draft":
        need = "edit_order"
    elif status in REVIEW_STATUSES:
        need = "confirm_review"
    else:
        raise HTTPException(status_code=400,
                            detail="แก้ไขได้เฉพาะฉบับร่าง หรือรายการที่อยู่ระหว่างตรวจสอบเท่านั้น")
    if not _has_perm(user, need):
        raise HTTPException(status_code=403,
                            detail=f"ไม่มีสิทธิ์แก้ไขรายการในขั้นตอนนี้ (role: {user['role']})")
    result = store.update_order(order_id, body.model_dump(exclude_unset=True))
    _log_stage("edit_order", "แก้ไข", o, order_id, user)
    return result


@app.delete("/api/orders/{order_id}")
def order_delete(order_id: str, user=Depends(_require_perm("delete"))):
    o = store.get_order(order_id)
    store.delete_order(order_id)
    analytics.invalidate_cache()
    store.log_activity("delete_order", user["email"], user["role"],
                       f"ลบ Order {(o or {}).get('order_no') or '-'}", order_id,
                       factory_id=_fid(user))
    return {"deleted": order_id}


@app.get("/api/export/status")
def export_status(user=Depends(auth.verify_token)):
    """How many approved orders have not yet been handed to SAP.

    Factory-wide for every role: any approver may hand over any approved order,
    so one who leaves cannot strand the orders they approved."""
    return store.export_status(factory_id=_fid(user))


@app.get("/api/export/batches")
def export_batches(limit: int = 20, user=Depends(auth.verify_token)):
    # Factory-wide, so approvers see what a colleague already sent to SAP.
    return {"batches": store.list_export_batches(limit, factory_id=_fid(user))}


@app.post("/api/export/batches/{batch_id}/undo")
def export_batch_undo(batch_id: str, user=Depends(admin_only)):
    """Un-mark a batch whose file never actually reached anyone."""
    cleared = store.undo_export_batch(batch_id)
    store.log_activity("export_undo", user["email"], user["role"],
                       f"ยกเลิกการทำเครื่องหมาย export {cleared} รายการ", batch_id)
    return {"cleared": cleared}


@app.get("/api/export")
def export(from_date: Optional[str] = None, to_date: Optional[str] = None,
          field: str = "document_date", status: str = "approved",
          only_new: bool = False, mark: bool = True,
          export_factory: Optional[str] = None,
          user=Depends(auth.verify_token)):
    from urllib.parse import quote
    # A reviewer's export must not carry orders nobody has approved yet.
    if user["role"] == "reviewer" and status != "approved":
        raise HTTPException(status_code=403,
                            detail="Reviewer Export ได้เฉพาะรายการที่อนุมัติแล้วเท่านั้น")
    if export_factory and user["role"] in ("super_admin", "admin"):
        fid = export_factory
    else:
        fid = _fid(user)
    data, _ = store.list_orders(limit=2000, factory_id=fid)
    if status and status != "all":
        data = [o for o in data if o.get("status") == status]
    if only_new:
        data = [o for o in data if not o.get("exported_at")]
    if field not in ("document_date", "scanned_at"):
        field = "document_date"
    if from_date or to_date:
        from datetime import datetime, timezone, timedelta
        _bkk = timezone(timedelta(hours=7))
        def datekey(o):
            v = o.get(field) or ""
            if not v:
                return ""
            if field == "scanned_at" and "T" in v:
                try:
                    dt = datetime.fromisoformat(v)
                    return dt.astimezone(_bkk).strftime("%Y-%m-%d")
                except Exception:
                    pass
            return v[:10]
        data = [o for o in data if datekey(o)
                and (not from_date or datekey(o) >= from_date)
                and (not to_date or datekey(o) <= to_date)]
    if not data:
        raise HTTPException(404, "ไม่พบรายการที่ตรงกับเงื่อนไข — ไม่มีอะไรให้ Export")
    xlsx = excel_export.build_workbook(data)

    # admin/super_admin exports are test-only — never mark orders as exported.
    # Only approver exports count as handed over to SAP.
    should_mark = mark and user["role"] == "approver"
    if should_mark:
        ids = [o["id"] for o in data if o.get("status") == "approved" and o.get("id")]
        batch_id = store.mark_exported(ids, user["email"], {
            "from_date": from_date, "to_date": to_date,
            "field": field, "status": status, "only_new": only_new,
        }, factory_id=fid)
        if ids:
            analytics.invalidate_cache()
            store.log_activity("export", user["email"], user["role"],
                               f"Export {len(ids)} รายการ", batch_id,
                               factory_id=fid)
    fac = store.get_factory(fid) if fid else None
    fac_code = fac["code"] if fac else "ALL"
    fac_name = fac["name"] if fac else "ทุกโรงงาน"
    thai = quote(f"ใบเบิกวัตถุดิบ_{fac_name}.xlsx")
    cd = f"attachment; filename=\"requisition_{fac_code}.xlsx\"; filename*=UTF-8''{thai}"
    return Response(
        content=xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": cd},
    )


# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Analytics: natural-language Q&A and material demand forecasting
# ---------------------------------------------------------------------------
class AskIn(BaseModel):
    question: str


@app.get("/api/ask/suggestions")
def ask_suggestions(user=Depends(_require_perm("ask"))):
    return {"suggestions": ask_ai.SUGGESTIONS}


@app.post("/api/ask")
def ask(body: AskIn, user=Depends(_require_perm("ask"))):
    settings = store.get_settings()
    provider = settings["provider"]
    key = _api_key(provider)
    if not key:
        raise HTTPException(400, "ยังไม่ได้ตั้งค่า API Key — กรุณาตั้งค่าในหน้า Settings")
    try:
        return ask_ai.ask(body.question, provider, key,
                          settings["models"][provider])
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        log.error("ask failed: %s", e, exc_info=True)
        raise HTTPException(502, _friendly_error(e))


@app.get("/api/analytics/forecast")
def analytics_forecast(months: int = 1, user=Depends(_require_perm("forecast"))):
    months = max(1, min(6, months))
    return analytics.forecast(months=months,
                              orders=analytics.load_orders(factory_id=_fid(user)))


@app.get("/api/analytics/bom")
def analytics_bom(user=Depends(_require_perm("forecast"))):
    return {"products": analytics.implied_bom(
        orders=analytics.load_orders(factory_id=_fid(user)))}


@app.get("/api/analytics/health")
def analytics_health(user=Depends(_require_perm("health"))):
    """Production health in one round trip — all four views share one order load."""
    orders = analytics.load_orders(factory_id=_fid(user))
    return {
        "yield": analytics.yield_trend(orders),
        "variance": analytics.plan_variance(orders),
        "workload": analytics.workload(orders),
        "weekday": analytics.weekday_pattern(orders),
    }


# ---------------------------------------------------------------------------
# Factory management (super_admin only)
# ---------------------------------------------------------------------------
class FactoryIn(BaseModel):
    code: str
    name: str


@app.get("/api/factories")
def list_factories(user=Depends(admin_only)):
    return {"factories": store.list_factories()}


@app.post("/api/factories")
def create_factory(body: FactoryIn, user=Depends(super_only)):
    code = (body.code or "").strip().upper()
    name = (body.name or "").strip()
    if not code or not name:
        raise HTTPException(status_code=400, detail="ต้องระบุรหัสและชื่อโรงงาน")
    if store.find_factory_by_code(code):
        raise HTTPException(status_code=409, detail=f"รหัส {code} มีอยู่แล้ว")
    fid = store.add_factory(code, name, user["email"])
    store.log_activity("create_factory", user["email"], user["role"],
                       f"สร้าง factory {code} ({name})")
    return {"id": fid, "code": code, "name": name}


@app.put("/api/factories/{factory_id}")
def update_factory(factory_id: str, body: FactoryIn, user=Depends(super_only)):
    fac = store.get_factory(factory_id)
    if not fac:
        raise HTTPException(status_code=404, detail="ไม่พบ factory")
    result = store.update_factory(factory_id, body.model_dump(exclude_unset=True))
    store.log_activity("update_factory", user["email"], user["role"],
                       f"แก้ไข factory {result['code']} ({result['name']})")
    return result


@app.delete("/api/factories/{factory_id}")
def delete_factory(factory_id: str, user=Depends(super_only)):
    fac = store.get_factory(factory_id)
    if not fac:
        raise HTTPException(status_code=404, detail="ไม่พบ factory")
    linked = store.factory_linked_collections(factory_id)
    if linked:
        raise HTTPException(status_code=409,
                            detail=f"ไม่สามารถลบได้ มีข้อมูลผูกอยู่: {', '.join(linked)}")
    store.delete_factory(factory_id)
    store.log_activity("delete_factory", user["email"], user["role"],
                       f"ลบ factory {fac['code']} ({fac['name']})")
    return {"ok": True}


@app.get("/api/permissions")
def get_permissions(user=Depends(admin_only)):
    return {"permissions": store.get_permissions()}


class PermissionsIn(BaseModel):
    permissions: dict


@app.post("/api/permissions")
def save_permissions(body: PermissionsIn, user=Depends(admin_only)):
    result = store.save_permissions(body.permissions)
    store.log_activity("change_permissions", user["email"], user["role"],
                       "เปลี่ยนสิทธิ์การเข้าถึงของ roles", factory_id=_fid(user))
    return {"permissions": result}


@app.get("/api/users")
def list_users(user=Depends(admin_only)):
    fid = _fid(user)
    return {"users": store.list_users(factory_id=fid)}


class UserCreate(BaseModel):
    email: str
    password: str
    role: str = "staff"
    display_name: Optional[str] = None
    factory_id: Optional[str] = None


MIN_PASSWORD = 6
# Public web API key (same one the login page ships) — used to prove the new
# credential really signs in before we tell the admin the account is ready.
WEB_API_KEY = os.environ.get("FIREBASE_WEB_API_KEY",
                             "AIzaSyASI7mYDFFM96FYF6PaAEU3yjX1TuCDccM")


def _can_sign_in(email, password):
    """Sign in as the new user once. Returns None on success, else the reason."""
    import json as _json
    import urllib.error
    import urllib.request
    url = ("https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
           f"?key={WEB_API_KEY}")
    payload = _json.dumps({"email": email, "password": password,
                           "returnSecureToken": True}).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return None
    except urllib.error.HTTPError as e:
        try:
            return (_json.loads(e.read())["error"]["message"])
        except Exception:  # noqa: BLE001
            return f"HTTP {e.code}"
    except Exception:  # noqa: BLE001
        return None  # network hiccup on our side — don't fail the creation


@app.post("/api/users")
def create_user(body: UserCreate, user=Depends(admin_only)):
    # Whitespace around either field is always a typo, and it is the usual
    # reason a freshly created account cannot log in.
    email = (body.email or "").strip().lower()
    password = (body.password or "").strip()
    if body.role not in store.VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"role ต้องเป็น {store.VALID_ROLES}")
    if "@" not in email:
        raise HTTPException(status_code=400, detail="รูปแบบอีเมลไม่ถูกต้อง")
    if len(password) < MIN_PASSWORD:
        raise HTTPException(status_code=400,
                            detail=f"รหัสผ่านต้องมีอย่างน้อย {MIN_PASSWORD} ตัวขึ้นไป")

    from firebase_admin import auth as fb_auth
    store._init()
    name = (body.display_name or "").strip() or email.split("@")[0]
    try:
        fb_user = fb_auth.create_user(email=email, password=password,
                                      display_name=name)
        uid = fb_user.uid
    except fb_auth.EmailAlreadyExistsError:
        # A half-finished attempt (auth account made, Firestore row missing)
        # must not become a dead end — adopt the account and set it up fully.
        existing = fb_auth.get_user_by_email(email)
        uid = existing.uid
        fb_auth.update_user(uid, password=password, display_name=name,
                            disabled=False)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"สร้างผู้ใช้ไม่สำเร็จ: {e}")

    # Determine factory for the new user
    target_fid = body.factory_id or _fid(user)
    fac = store.get_factory(target_fid) if target_fid else None
    if body.role != "super_admin" and not target_fid:
        raise HTTPException(status_code=400,
                            detail="ต้องระบุ factory สำหรับผู้ใช้ที่ไม่ใช่ super_admin")
    store._create_user_doc(uid, email, body.role, user["email"],
                           factory_id=target_fid,
                           factory_code=fac["code"] if fac else None,
                           factory_name=fac["name"] if fac else None)
    store.log_activity("create_user", user["email"], user["role"],
                       f"สร้างผู้ใช้ {email} (role: {body.role})",
                       factory_id=_fid(user))

    problem = _can_sign_in(email, password)
    if problem:
        raise HTTPException(
            status_code=400,
            detail=f"สร้างบัญชีแล้ว แต่ยังเข้าสู่ระบบไม่ได้ ({problem}) — "
                   "ลองตั้งรหัสผ่านใหม่จากปุ่มกุญแจในตาราง")
    return {"uid": uid, "email": email, "role": body.role, "verified": True,
            "factory_id": target_fid,
            "factory_code": fac["code"] if fac else None}


class UserUpdate(BaseModel):
    role: Optional[str] = None
    factory_id: Optional[str] = None


@app.put("/api/users/{uid}")
def update_user(uid: str, body: UserUpdate, user=Depends(admin_only)):
    existing = store.get_user_doc(uid)
    if not existing:
        raise HTTPException(status_code=404, detail="ไม่พบผู้ใช้")
    if body.role:
        if body.role not in store.VALID_ROLES:
            raise HTTPException(status_code=400, detail=f"role ต้องเป็น {store.VALID_ROLES}")
        store.update_user_role(uid, body.role)
        store.log_activity("change_role", user["email"], user["role"],
                           f"เปลี่ยน role ของ {existing['email']} เป็น {body.role}",
                           factory_id=_fid(user))
    if body.factory_id and user["role"] == "super_admin":
        fac = store.get_factory(body.factory_id)
        if not fac:
            raise HTTPException(status_code=400, detail="ไม่พบ factory")
        store.update_user_factory(uid, body.factory_id, fac["code"], fac["name"])
        store.log_activity("change_factory", user["email"], user["role"],
                           f"ย้าย {existing['email']} ไป factory {fac['code']}")
    return store.get_user_doc(uid)


@app.delete("/api/users/{uid}")
def delete_user(uid: str, user=Depends(admin_only)):
    existing = store.get_user_doc(uid)
    if not existing:
        raise HTTPException(status_code=404, detail="ไม่พบผู้ใช้")
    if uid == user["uid"]:
        raise HTTPException(status_code=400, detail="ไม่สามารถลบตัวเองได้")
    try:
        from firebase_admin import auth as fb_auth
        store._init()
        fb_auth.delete_user(uid)
    except Exception:  # noqa: BLE001
        pass
    store.delete_user_doc(uid)
    store.log_activity("delete_user", user["email"], user["role"],
                       f"ลบผู้ใช้ {existing['email']}")
    return {"deleted": uid}


class ResetPwIn(BaseModel):
    password: str


@app.post("/api/users/{uid}/reset-password")
def reset_user_pw(uid: str, body: ResetPwIn, user=Depends(admin_only)):
    existing = store.get_user_doc(uid)
    if not existing:
        raise HTTPException(status_code=404, detail="ไม่พบผู้ใช้")
    password = (body.password or "").strip()
    if len(password) < MIN_PASSWORD:
        raise HTTPException(status_code=400,
                            detail=f"รหัสผ่านต้องมีอย่างน้อย {MIN_PASSWORD} ตัวขึ้นไป")
    try:
        from firebase_admin import auth as fb_auth
        store._init()
        fb_auth.update_user(uid, password=password, disabled=False)
        store.log_activity("reset_password", user["email"], user["role"],
                           f"รีเซ็ตรหัสผ่าน {existing['email']}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"รีเซ็ตรหัสผ่านไม่สำเร็จ: {e}")

    problem = _can_sign_in((existing.get("email") or "").strip().lower(), password)
    if problem:
        raise HTTPException(status_code=400,
                            detail=f"ตั้งรหัสผ่านแล้ว แต่ยังเข้าสู่ระบบไม่ได้ ({problem})")
    return {"ok": True, "verified": True}


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------
@app.get("/api/activity")
def activity_log(limit: int = 100, user=Depends(_require_perm("activity"))):
    return {"logs": store.list_activity(limit=min(limit, 500), factory_id=_fid(user))}


# ---------------------------------------------------------------------------
# Dead letter queue (admin only)
# ---------------------------------------------------------------------------
@app.get("/api/dead-letter")
def dead_letter(user=Depends(auth.verify_token)):
    items = store.list_dead_letter(factory_id=_fid(user))
    if user["role"] == "staff":
        items = [d for d in items if d.get("uploaded_by") == user["email"]]
    return {"items": items}


@app.post("/api/dead-letter/{dlq_id}/retry")
def retry_dlq(dlq_id: str, user=Depends(auth.verify_token)):
    dl = store.get_dead_letter(dlq_id)
    if not dl:
        raise HTTPException(status_code=404, detail="ไม่พบรายการใน dead letter queue")
    if user["role"] == "staff" and dl.get("uploaded_by") != user["email"]:
        raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์ดำเนินการรายการนี้")
    result = store.retry_dead_letter(dlq_id)
    if not result:
        raise HTTPException(status_code=404, detail="ไม่พบรายการใน dead letter queue")
    store.log_activity("retry_dead_letter", user["email"], user["role"],
                       f"ส่ง {dlq_id} กลับไปคิวใหม่")
    return {"retried": dlq_id}


@app.delete("/api/dead-letter/{dlq_id}")
def delete_dlq(dlq_id: str, user=Depends(auth.verify_token)):
    dl = store.get_dead_letter(dlq_id)
    if not dl:
        raise HTTPException(status_code=404, detail="ไม่พบรายการใน dead letter queue")
    if user["role"] == "staff" and dl.get("uploaded_by") != user["email"]:
        raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์ดำเนินการรายการนี้")
    if not store.delete_dead_letter(dlq_id):
        raise HTTPException(status_code=404, detail="ไม่พบรายการใน dead letter queue")
    return {"deleted": dlq_id}


# ---------------------------------------------------------------------------
# Log cleanup
# ---------------------------------------------------------------------------
@app.post("/api/admin/cleanup-logs")
def cleanup_logs(user=Depends(admin_only)):
    deleted = store.cleanup_old_logs()
    store.log_activity("cleanup_logs", user["email"], user["role"],
                       f"ลบ activity log เก่า {deleted} รายการ (>{store.LOG_RETENTION_DAYS} วัน)")
    return {"deleted": deleted, "retention_days": store.LOG_RETENTION_DAYS}


class FlushIn(BaseModel):
    scope: str = "mock"                 # "mock" = ข้อมูลทดสอบเท่านั้น, "all" = ทั้งหมด
    include_activity: bool = False
    confirm: str = ""


@app.get("/api/admin/flush/preview")
def flush_preview(scope: str = "mock", include_activity: bool = False,
                  user=Depends(admin_only)):
    """What a flush would remove. Always shown before the destructive call."""
    return {
        "scope": scope,
        "counts": store.flush_preview(mock_only=(scope != "all"),
                                      include_activity=include_activity),
        "confirm_phrase": store.FLUSH_CONFIRM,
    }


@app.post("/api/admin/flush")
def flush(body: FlushIn, user=Depends(admin_only)):
    """Delete scanned data. Irreversible, admin only, and typed-confirmation gated."""
    if body.confirm.strip() != store.FLUSH_CONFIRM:
        raise HTTPException(400, f"กรุณาพิมพ์คำว่า “{store.FLUSH_CONFIRM}” "
                                 "ให้ถูกต้องเพื่อยืนยันการลบ")
    if body.scope not in ("mock", "all"):
        raise HTTPException(400, "ขอบเขตการลบไม่ถูกต้อง")

    mock_only = body.scope != "all"
    deleted = store.flush_data(mock_only=mock_only,
                               include_activity=body.include_activity)
    analytics.invalidate_cache()
    # Logged after the fact, so the audit trail survives even a full wipe.
    store.log_activity("flush_data", user["email"], user["role"],
                       f"ล้างข้อมูล ({'ทั้งหมด' if not mock_only else 'เฉพาะข้อมูลทดสอบ'}): "
                       f"ใบสั่งผลิต {deleted['orders']} · คิว {deleted['pending']} · "
                       f"ที่ล้มเหลว {deleted['dead_letter']} · รูป {deleted['images']}")
    return {"deleted": deleted, "scope": body.scope}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
