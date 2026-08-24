"""Cloud Run backend (FastAPI) for the ใบเบิกวัตถุดิบ scanner.

Flow: mobile web uploads a photo → /api/scan extracts with a vision model →
saves the image to Cloud Storage and the structured record to Firestore →
/api/export builds an .xlsx from Firestore on demand.
"""
import io
import logging
import os
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

admin_only = require_role("admin")
admin_or_sup = require_role("admin", "supervisor")
any_role = require_role("admin", "supervisor", "staff")

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
    perms = store.get_permissions()
    cfg["permissions"] = perms.get(user["role"], [])
    cfg["all_permissions"] = perms
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


@app.post("/api/scan")
async def scan(file: UploadFile = File(...), user=Depends(auth.verify_token)):
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

    dup = store.find_by_order_no(data.get("order_no"))
    if dup:
        raise HTTPException(status_code=409,
                            detail=f"Order No. {dup['order_no']} มีในระบบแล้ว")

    blob_path = store.upload_image(optimized, opt_ct, file.filename)
    ai_path = (store.upload_image(ai_bytes[1], ai_bytes[0], "ai_" + (file.filename or "page"))
               if ai_bytes else None)
    order_id = store.add_order(data, blob_path, provider, user_email=user["email"],
                               ai_image=ai_path)
    analytics.invalidate_cache()
    store.log_activity("scan", user["email"], user["role"],
                       f"สแกนไฟล์ {file.filename} → Order {data.get('order_no') or '-'}", order_id)
    return {"id": order_id, "data": data,
            "summary": {"lines": len(data.get("lines", []))}}


def _process_queue(trigger="manual"):
    """Scan every pending/failed file → Firestore. Shared by manual button and scheduler."""
    settings = store.get_settings()
    provider = settings["provider"]
    model = settings["models"].get(provider, "")
    key = _api_key(provider)
    if not key:
        return {"error": f"ยังไม่ได้ตั้งค่า API key ของ {provider}", "processed": 0}
    result = {"processed": 0, "succeeded": 0, "failed": 0, "dead": 0, "items": []}
    for p in store.list_pending():
        if p.get("status") not in ("pending", "failed"):
            continue
        item = {"file": p.get("filename"), "status": "", "detail": ""}
        try:
            raw = store.download_bytes(p["storage_path"])
            images, _ai = _prepare_images(raw, p.get("content_type") or "", p.get("filename") or "")
            data = extractor.extract(images, provider, key, model)
            dup = store.find_by_order_no(data.get("order_no"))
            if dup:
                store.delete_pending(p["id"])
                item["status"] = "skipped"
                item["detail"] = f"Order No. {data.get('order_no')} มีในระบบแล้ว — ข้าม"
                result["skipped"] = result.get("skipped", 0) + 1
                result["processed"] += 1
                result["items"].append(item)
                continue
            store.add_order(data, p["storage_path"], provider,
                            user_email=p.get("uploaded_by"), source_filename=p.get("filename"))
            store.delete_pending(p["id"])
            item["status"] = "success"
            item["detail"] = f"Order {data.get('order_no') or '-'} · {len(data.get('lines', []))} รายการ"
            result["succeeded"] += 1
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
            store.log_run(trigger, result)
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
    saved = []
    for f in files:
        raw = await f.read()
        optimized, opt_ct = _optimize_image(raw, f.content_type or "")
        store.add_pending(optimized, opt_ct, f.filename, user["email"])
        saved.append(f.filename)
    store.log_activity("upload_queue", user["email"], user["role"],
                       f"อัปโหลด {len(saved)} ไฟล์เข้าคิว")
    return {"queued": saved, "count": len(saved)}


@app.get("/api/pending")
def pending(user=Depends(auth.verify_token)):
    return {"pending": store.list_pending()}


@app.get("/api/pending/{pid}/preview")
def pending_preview(pid: str, user=Depends(auth.verify_token)):
    p = store.get_pending(pid)
    if not p:
        raise HTTPException(status_code=404, detail="ไม่พบไฟล์ในคิว")
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
    r = _process_queue()
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
    if p.get("status") != "pending":
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
        dup = store.find_by_order_no(data.get("order_no"))
        if dup:
            store.delete_pending(pid)
            return {"status": "skipped", "detail": f"Order No. {data.get('order_no')} มีในระบบแล้ว — ข้าม"}
        store.add_order(data, p["storage_path"], provider,
                        user_email=p.get("uploaded_by"), source_filename=p.get("filename"))
        store.delete_pending(pid)
        store.log_activity("scan", user["email"], user["role"],
                           f"สแกน {p.get('filename')} → Order {data.get('order_no')}")
        return {"status": "success", "detail": f"Order {data.get('order_no') or '-'} · {len(data.get('lines', []))} รายการ"}
    except Exception as e:  # noqa: BLE001
        store.fail_pending(pid, _friendly_error(e))
        return {"status": "failed", "detail": _friendly_error(e)}


@app.post("/api/cron/process")
def cron_process(x_cron_key: str = Header(default="")):
    secret = os.environ.get("CRON_SECRET", "")
    if not secret or x_cron_key != secret:
        raise HTTPException(status_code=403, detail="invalid cron key")
    pulled = _pull_drive()
    r = _process_queue(trigger="schedule")
    r["drive_pulled"] = pulled.get("pulled", 0)
    try:
        r["logs_cleaned"] = store.cleanup_old_logs()
    except Exception:  # noqa: BLE001
        pass
    return r


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
    orders, _ = store.list_orders(limit=500)
    today = datetime.datetime.utcnow().date().isoformat()
    today_count = sum(1 for o in orders if (o.get("scanned_at") or "").startswith(today))
    return {
        "total_scanned": store.count_orders(),
        "today_scanned": today_count,
        "runs": store.list_runs(limit=20),
        "recent": [
            {"filename": o.get("source_filename"),
             "order_no": o.get("order_no"),
             "lines": len(o.get("lines", [])),
             "scanned_at": o.get("scanned_at"),
             "scanned_by": o.get("scanned_by")}
            for o in orders[:20]
        ],
    }


@app.get("/api/orders")
def orders(limit: int = 100, cursor: Optional[str] = None,
           user=Depends(auth.verify_token)):
    items, next_cursor = store.list_orders(limit=min(limit, 500), cursor=cursor)
    return {"orders": items, "next_cursor": next_cursor}


@app.get("/api/orders/{order_id}")
def order_detail(order_id: str, user=Depends(auth.verify_token)):
    o = store.get_order(order_id)
    if not o:
        raise HTTPException(status_code=404, detail="ไม่พบรายการ")
    o["has_image"] = bool(o.get("source_image"))
    return o


@app.get("/api/orders/{order_id}/image")
def order_image(order_id: str, variant: str = "source",
                user=Depends(auth.verify_token)):
    """variant=ai returns the page as the model saw it, when cropping altered it."""
    o = store.get_order(order_id)
    field = "ai_image" if variant == "ai" else "source_image"
    if not o or not o.get(field):
        raise HTTPException(status_code=404, detail="ไม่พบรูปภาพ")
    try:
        raw = store.download_bytes(o[field])
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


@app.post("/api/orders/{order_id}/approve")
def order_approve(order_id: str, user=Depends(_require_perm("approve"))):
    o = store.get_order(order_id)
    if not o:
        raise HTTPException(status_code=404, detail="ไม่พบรายการ")
    result = store.approve_order(order_id, user["email"])
    analytics.invalidate_cache()
    store.log_activity("approve", user["email"], user["role"],
                       f"อนุมัติ Order {o.get('order_no') or '-'}", order_id)
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
    o = store.get_order(order_id)
    if not o:
        raise HTTPException(status_code=404, detail="ไม่พบรายการ")
    result = store.update_order(order_id, body.model_dump(exclude_unset=True))
    analytics.invalidate_cache()
    store.log_activity("edit_order", user["email"], user["role"],
                       f"แก้ไข Order {o.get('order_no') or '-'}", order_id)
    return result


@app.delete("/api/orders/{order_id}")
def order_delete(order_id: str, user=Depends(_require_perm("delete"))):
    o = store.get_order(order_id)
    store.delete_order(order_id)
    analytics.invalidate_cache()
    store.log_activity("delete_order", user["email"], user["role"],
                       f"ลบ Order {(o or {}).get('order_no') or '-'}", order_id)
    return {"deleted": order_id}


@app.get("/api/export/status")
def export_status(user=Depends(auth.verify_token)):
    """How many approved orders have not yet been handed to SAP."""
    return store.export_status()


@app.get("/api/export/batches")
def export_batches(limit: int = 20, user=Depends(auth.verify_token)):
    return {"batches": store.list_export_batches(limit)}


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
          user=Depends(auth.verify_token)):
    from urllib.parse import quote
    data, _ = store.list_orders(limit=2000)
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

    # Only approved orders count as handed over; a draft in the file is a
    # preview, and marking it would hide it from the "not yet sent" count.
    if mark:
        ids = [o["id"] for o in data if o.get("status") == "approved" and o.get("id")]
        batch_id = store.mark_exported(ids, user["email"], {
            "from_date": from_date, "to_date": to_date,
            "field": field, "status": status, "only_new": only_new,
        })
        if ids:
            analytics.invalidate_cache()
            store.log_activity("export", user["email"], user["role"],
                               f"Export {len(ids)} รายการ", batch_id)
    # HTTP headers are latin-1 only, so encode the Thai filename per RFC 5987.
    thai = quote("ใบเบิกวัตถุดิบ.xlsx")
    cd = f"attachment; filename=\"requisition.xlsx\"; filename*=UTF-8''{thai}"
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
    return analytics.forecast(months=months)


@app.get("/api/analytics/bom")
def analytics_bom(user=Depends(_require_perm("forecast"))):
    return {"products": analytics.implied_bom()}


@app.get("/api/analytics/health")
def analytics_health(user=Depends(_require_perm("health"))):
    """Production health in one round trip — all four views share one order load."""
    orders = analytics.load_orders()
    return {
        "yield": analytics.yield_trend(orders),
        "variance": analytics.plan_variance(orders),
        "workload": analytics.workload(orders),
        "weekday": analytics.weekday_pattern(orders),
    }


@app.get("/api/permissions")
def get_permissions(user=Depends(admin_only)):
    return {"permissions": store.get_permissions()}


class PermissionsIn(BaseModel):
    permissions: dict


@app.post("/api/permissions")
def save_permissions(body: PermissionsIn, user=Depends(admin_only)):
    result = store.save_permissions(body.permissions)
    store.log_activity("change_permissions", user["email"], user["role"],
                       "เปลี่ยนสิทธิ์การเข้าถึงของ roles")
    return {"permissions": result}


@app.get("/api/users")
def list_users(user=Depends(admin_only)):
    return {"users": store.list_users()}


class UserCreate(BaseModel):
    email: str
    password: str
    role: str = "staff"
    display_name: Optional[str] = None


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

    store._create_user_doc(uid, email, body.role, user["email"])
    store.log_activity("create_user", user["email"], user["role"],
                       f"สร้างผู้ใช้ {email} (role: {body.role})")

    problem = _can_sign_in(email, password)
    if problem:
        raise HTTPException(
            status_code=400,
            detail=f"สร้างบัญชีแล้ว แต่ยังเข้าสู่ระบบไม่ได้ ({problem}) — "
                   "ลองตั้งรหัสผ่านใหม่จากปุ่มกุญแจในตาราง")
    return {"uid": uid, "email": email, "role": body.role, "verified": True}


class UserUpdate(BaseModel):
    role: Optional[str] = None


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
                           f"เปลี่ยน role ของ {existing['email']} เป็น {body.role}")
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
    return {"logs": store.list_activity(limit=min(limit, 500))}


# ---------------------------------------------------------------------------
# Dead letter queue (admin only)
# ---------------------------------------------------------------------------
@app.get("/api/dead-letter")
def dead_letter(user=Depends(admin_only)):
    return {"items": store.list_dead_letter()}


@app.post("/api/dead-letter/{dlq_id}/retry")
def retry_dlq(dlq_id: str, user=Depends(admin_only)):
    result = store.retry_dead_letter(dlq_id)
    if not result:
        raise HTTPException(status_code=404, detail="ไม่พบรายการใน dead letter queue")
    store.log_activity("retry_dead_letter", user["email"], user["role"],
                       f"ส่ง {dlq_id} กลับไปคิวใหม่")
    return {"retried": dlq_id}


@app.delete("/api/dead-letter/{dlq_id}")
def delete_dlq(dlq_id: str, user=Depends(admin_only)):
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
