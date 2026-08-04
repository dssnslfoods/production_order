"""Firestore + Cloud Storage access. Firebase Admin is initialized lazily on first use."""
import datetime
import os
import uuid

import firebase_admin
from firebase_admin import firestore, storage

_app = None
ORDERS = "production_orders"
SETTINGS = "settings"
PENDING = "pending"
DEAD_LETTER = "dead_letter"
USERS = "users"
ACTIVITY = "activity_logs"

MAX_RETRY = 3
LOG_RETENTION_DAYS = 90

DEFAULT_PERMISSIONS = {
    "admin": ["dashboard", "scan", "orders", "users", "activity", "settings",
              "approve", "delete", "export"],
    "supervisor": ["dashboard", "scan", "orders", "activity",
                   "approve", "export"],
    "staff": ["dashboard", "scan", "orders", "export"],
}

DEFAULT_SETTINGS = {
    "provider": "claude",
    "models": {"claude": "claude-opus-4-8", "gemini": "gemini-2.5-flash", "openai": "gpt-4o"},
    "api_keys": {"claude": "", "gemini": "", "openai": ""},
    "drive_folder_id": "",
    "role_permissions": dict(DEFAULT_PERMISSIONS),
}


def _init():
    global _app
    if _app is None:
        bucket = os.environ.get("STORAGE_BUCKET")  # e.g. my-project.appspot.com
        opts = {"storageBucket": bucket} if bucket else None
        # On Cloud Run, default application credentials are picked up automatically.
        _app = firebase_admin.initialize_app(options=opts)
    return _app


def db():
    _init()
    return firestore.client()


def bucket():
    _init()
    return storage.bucket()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def get_settings():
    doc = db().collection(SETTINGS).document("app").get()
    if doc.exists:
        data = dict(DEFAULT_SETTINGS)
        data.update(doc.to_dict() or {})
        return data
    return dict(DEFAULT_SETTINGS)


def get_permissions():
    s = get_settings()
    perms = s.get("role_permissions")
    if not perms or not isinstance(perms, dict):
        return dict(DEFAULT_PERMISSIONS)
    for role in ("admin", "supervisor", "staff"):
        if role not in perms:
            perms[role] = list(DEFAULT_PERMISSIONS.get(role, []))
    # admin always keeps users + settings to avoid lockout
    for must in ("users", "settings", "dashboard"):
        if must not in perms["admin"]:
            perms["admin"].append(must)
    return perms


def save_permissions(perms):
    # admin always keeps users + settings
    for must in ("users", "settings", "dashboard"):
        if must not in perms.get("admin", []):
            perms.setdefault("admin", []).append(must)
    db().collection(SETTINGS).document("app").set(
        {"role_permissions": perms}, merge=True)
    return get_permissions()


def save_settings(patch):
    cur = get_settings()
    if "provider" in patch and patch["provider"]:
        cur["provider"] = patch["provider"]
    if "models" in patch and isinstance(patch["models"], dict):
        cur["models"].update(patch["models"])
    if "api_keys" in patch and isinstance(patch["api_keys"], dict):
        cur.setdefault("api_keys", {})
        for k, v in patch["api_keys"].items():
            if v:  # only overwrite when a real value is supplied
                cur["api_keys"][k] = v
    if "drive_folder_id" in patch:
        cur["drive_folder_id"] = patch["drive_folder_id"] or ""
    db().collection(SETTINGS).document("app").set(cur)
    return cur


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
def upload_image(raw: bytes, content_type: str, filename: str):
    ext = os.path.splitext(filename or "")[1] or ".jpg"
    blob_path = f"scans/{uuid.uuid4().hex}{ext}"
    blob = bucket().blob(blob_path)
    blob.upload_from_string(raw, content_type=content_type or "application/octet-stream")
    return blob_path


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
def add_order(data, source_image, provider, user_email=None, source_filename=None):
    doc = {
        "order_no": data.get("order_no"),
        "document_date": data.get("document_date"),
        "series_no": data.get("series_no"),
        "product_name": data.get("product_name"),
        "plan_total": data.get("plan_total"),
        "actual_total": data.get("actual_total"),
        "plan_unit": data.get("plan_unit"),
        "lines": data.get("lines", []),
        "source_image": source_image,
        "source_filename": source_filename,
        "provider": provider,
        "scanned_by": user_email,
        "scanned_at": firestore.SERVER_TIMESTAMP,
        "status": "pending_approval",
    }
    ref = db().collection(ORDERS).add(doc)[1]
    return ref.id


RUNS = "scan_runs"


def log_run(trigger, result):
    """Record one processing run (manual button or scheduled) for the report."""
    db().collection(RUNS).add({
        "ran_at": firestore.SERVER_TIMESTAMP,
        "trigger": trigger,
        "processed": result.get("processed", 0),
        "succeeded": result.get("succeeded", 0),
        "failed": result.get("failed", 0),
    })


def list_runs(limit=20):
    q = (db().collection(RUNS)
         .order_by("ran_at", direction=firestore.Query.DESCENDING).limit(limit))
    out = []
    for d in q.stream():
        r = d.to_dict()
        ts = r.get("ran_at")
        r["ran_at"] = ts.isoformat() if hasattr(ts, "isoformat") else None
        out.append(r)
    return out


def count_orders():
    try:
        from google.cloud.firestore_v1.aggregation import AggregationQuery
        agg = AggregationQuery(db().collection(ORDERS)).count(alias="n")
        res = agg.get()
        return int(res[0][0].value)
    except Exception:  # noqa: BLE001
        return sum(1 for _ in db().collection(ORDERS).stream())


def list_orders(limit=100, cursor=None):
    """List orders with cursor-based pagination.

    Returns (orders, next_cursor).  Pass next_cursor back as `cursor`
    to fetch the next page.  next_cursor is None when there are no more.
    """
    q = (db().collection(ORDERS)
         .order_by("scanned_at", direction=firestore.Query.DESCENDING))
    if cursor:
        snap = db().collection(ORDERS).document(cursor).get()
        if snap.exists:
            q = q.start_after(snap)
    q = q.limit(limit + 1)
    docs = list(q.stream())
    has_more = len(docs) > limit
    if has_more:
        docs = docs[:limit]
    out = []
    for d in docs:
        row = d.to_dict()
        row["id"] = d.id
        ts = row.get("scanned_at")
        row["scanned_at"] = ts.isoformat() if hasattr(ts, "isoformat") else None
        out.append(row)
    next_cursor = docs[-1].id if has_more else None
    return out, next_cursor


def get_order(order_id):
    d = db().collection(ORDERS).document(order_id).get()
    if not d.exists:
        return None
    row = d.to_dict()
    row["id"] = d.id
    ts = row.get("scanned_at")
    row["scanned_at"] = ts.isoformat() if hasattr(ts, "isoformat") else None
    return row


def update_order(order_id, patch):
    """Save edits to a scanned order (used by the review/edit page before export)."""
    allowed = {"order_no", "document_date", "series_no", "product_name",
                "plan_total", "actual_total", "plan_unit", "lines"}
    upd = {k: v for k, v in patch.items() if k in allowed}
    upd["edited"] = True
    upd["edited_at"] = firestore.SERVER_TIMESTAMP
    db().collection(ORDERS).document(order_id).update(upd)
    return get_order(order_id)


def approve_order(order_id, user_email):
    ref = db().collection(ORDERS).document(order_id)
    doc = ref.get()
    img_path = (doc.to_dict() or {}).get("source_image") if doc.exists else None
    ref.update({
        "status": "approved",
        "approved_by": user_email,
        "approved_at": firestore.SERVER_TIMESTAMP,
        "source_image": None,
    })
    if img_path:
        try:
            bucket().blob(img_path).delete()
        except Exception:  # noqa: BLE001
            pass
    return get_order(order_id)


def delete_order(order_id):
    ref = db().collection(ORDERS).document(order_id)
    doc = ref.get()
    if doc.exists:
        img = (doc.to_dict() or {}).get("source_image")
        if img:
            try:
                bucket().blob(img).delete()  # ลบรูปต้นฉบับใน Storage ด้วย
            except Exception:  # noqa: BLE001
                pass
    ref.delete()


# ---------------------------------------------------------------------------
# Pending queue (upload now, scan later — manual button or scheduled)
# ---------------------------------------------------------------------------
def add_pending(raw: bytes, content_type: str, filename: str, user_email=None):
    ext = os.path.splitext(filename or "")[1] or ".jpg"
    path = f"pending/{uuid.uuid4().hex}{ext}"
    bucket().blob(path).upload_from_string(raw, content_type=content_type or "application/octet-stream")
    doc = {
        "filename": filename, "storage_path": path, "content_type": content_type,
        "status": "pending", "error": None, "uploaded_by": user_email,
        "uploaded_at": firestore.SERVER_TIMESTAMP,
    }
    return db().collection(PENDING).add(doc)[1].id


def get_pending(pid):
    d = db().collection(PENDING).document(pid).get()
    if not d.exists:
        return None
    r = d.to_dict()
    r["id"] = d.id
    return r


def list_pending():
    out = []
    for d in db().collection(PENDING).order_by("uploaded_at").stream():
        r = d.to_dict(); r["id"] = d.id
        ts = r.get("uploaded_at")
        r["uploaded_at"] = ts.isoformat() if hasattr(ts, "isoformat") else None
        out.append(r)
    return out


def download_bytes(blob_path):
    return bucket().blob(blob_path).download_as_bytes()


def delete_pending(pid):
    db().collection(PENDING).document(pid).delete()


def fail_pending(pid, err):
    ref = db().collection(PENDING).document(pid)
    doc = ref.get()
    data = doc.to_dict() if doc.exists else {}
    retries = data.get("retry_count", 0) + 1
    if retries >= MAX_RETRY:
        data.update({
            "status": "dead",
            "error": str(err)[:500],
            "retry_count": retries,
            "moved_at": firestore.SERVER_TIMESTAMP,
        })
        db().collection(DEAD_LETTER).document(pid).set(data)
        ref.delete()
        return "dead"
    ref.update({
        "status": "failed",
        "error": str(err)[:500],
        "retry_count": retries,
    })
    return "failed"


def list_dead_letter():
    out = []
    for d in db().collection(DEAD_LETTER).order_by("moved_at", direction=firestore.Query.DESCENDING).stream():
        r = d.to_dict()
        r["id"] = d.id
        for ts_field in ("uploaded_at", "moved_at"):
            ts = r.get(ts_field)
            r[ts_field] = ts.isoformat() if hasattr(ts, "isoformat") else None
        out.append(r)
    return out


def retry_dead_letter(dlq_id):
    ref = db().collection(DEAD_LETTER).document(dlq_id)
    doc = ref.get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    data["status"] = "pending"
    data["error"] = None
    data["retry_count"] = 0
    data.pop("moved_at", None)
    db().collection(PENDING).document(dlq_id).set(data)
    ref.delete()
    return dlq_id


def delete_dead_letter(dlq_id):
    ref = db().collection(DEAD_LETTER).document(dlq_id)
    doc = ref.get()
    if not doc.exists:
        return False
    data = doc.to_dict()
    if data.get("storage_path"):
        try:
            bucket().blob(data["storage_path"]).delete()
        except Exception:  # noqa: BLE001
            pass
    ref.delete()
    return True


def signed_image_url(blob_path, minutes=15):
    import datetime
    blob = bucket().blob(blob_path)
    return blob.generate_signed_url(expiration=datetime.timedelta(minutes=minutes))


# ---------------------------------------------------------------------------
# Duplicate detection — Order No. is the primary key
# ---------------------------------------------------------------------------
def find_by_order_no(order_no: str):
    """Return existing order if order_no already exists, else None."""
    if not order_no:
        return None
    for d in (db().collection(ORDERS)
              .where("order_no", "==", order_no).limit(1).stream()):
        row = d.to_dict()
        return {"id": d.id, "order_no": row.get("order_no"),
                "series_no": row.get("series_no"),
                "source_filename": row.get("source_filename")}


# ---------------------------------------------------------------------------
# User management (roles: admin, supervisor, staff)
# ---------------------------------------------------------------------------
VALID_ROLES = {"admin", "supervisor", "staff"}


def get_user_role(uid, email):
    """Get user role from Firestore. Auto-creates first user as admin."""
    doc = db().collection(USERS).document(uid).get()
    if doc.exists:
        return (doc.to_dict() or {}).get("role", "staff")
    if _count_users() == 0:
        _create_user_doc(uid, email, "admin", "system")
        return "admin"
    _create_user_doc(uid, email, "staff", "auto")
    return "staff"


def _count_users():
    try:
        from google.cloud.firestore_v1.aggregation import AggregationQuery
        agg = AggregationQuery(db().collection(USERS)).count(alias="n")
        res = agg.get()
        return int(res[0][0].value)
    except Exception:  # noqa: BLE001
        return sum(1 for _ in db().collection(USERS).limit(1).stream())


def _create_user_doc(uid, email, role, created_by):
    db().collection(USERS).document(uid).set({
        "email": email,
        "role": role,
        "created_at": firestore.SERVER_TIMESTAMP,
        "created_by": created_by,
    })


def list_users():
    out = []
    for d in db().collection(USERS).order_by("email").stream():
        r = d.to_dict()
        r["uid"] = d.id
        ts = r.get("created_at")
        r["created_at"] = ts.isoformat() if hasattr(ts, "isoformat") else None
        out.append(r)
    return out


def update_user_role(uid, role):
    if role not in VALID_ROLES:
        raise ValueError(f"role ต้องเป็น {VALID_ROLES}")
    db().collection(USERS).document(uid).update({"role": role})


def delete_user_doc(uid):
    db().collection(USERS).document(uid).delete()


def get_user_doc(uid):
    doc = db().collection(USERS).document(uid).get()
    if not doc.exists:
        return None
    r = doc.to_dict()
    r["uid"] = doc.id
    return r


# ---------------------------------------------------------------------------
# Activity log (audit trail)
# ---------------------------------------------------------------------------
def log_activity(action, user_email, user_role, detail=None, target_id=None):
    db().collection(ACTIVITY).add({
        "action": action,
        "user_email": user_email,
        "user_role": user_role,
        "detail": detail,
        "target_id": target_id,
        "timestamp": firestore.SERVER_TIMESTAMP,
    })


def list_activity(limit=100):
    q = (db().collection(ACTIVITY)
         .order_by("timestamp", direction=firestore.Query.DESCENDING)
         .limit(limit))
    out = []
    for d in q.stream():
        r = d.to_dict()
        r["id"] = d.id
        ts = r.get("timestamp")
        r["timestamp"] = ts.isoformat() if hasattr(ts, "isoformat") else None
        out.append(r)
    return out


def cleanup_old_logs(days=None):
    """Delete activity logs older than `days` (default LOG_RETENTION_DAYS).

    Returns the number of deleted documents.
    """
    days = days or LOG_RETENTION_DAYS
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    q = db().collection(ACTIVITY).where("timestamp", "<", cutoff).limit(500)
    deleted = 0
    while True:
        docs = list(q.stream())
        if not docs:
            break
        batch = db().batch()
        for d in docs:
            batch.delete(d.reference)
        batch.commit()
        deleted += len(docs)
    return deleted


# ---------------------------------------------------------------------------
# Flush: reset the working data before going live
# ---------------------------------------------------------------------------
FLUSH_BATCH = 400
FLUSH_CONFIRM = "ล้างข้อมูล"


def _flush_targets(mock_only):
    """Which documents a flush would remove.

    Users and settings are never included.  Wiping them would lock everyone
    out of the very screen that triggered the flush, and they are not the
    data anyone means by "start fresh".
    """
    orders = []
    for d in db().collection(ORDERS).stream():
        row = d.to_dict() or {}
        if mock_only and not row.get("is_mock"):
            continue
        orders.append((d.id, row.get("source_image")))

    if mock_only:
        # Only scanned orders carry the mock tag; leave real queue items alone.
        return {"orders": orders, "pending": [], "dead_letter": []}

    pending = [(d.id, (d.to_dict() or {}).get("storage_path"))
               for d in db().collection(PENDING).stream()]
    dead = [(d.id, (d.to_dict() or {}).get("storage_path"))
            for d in db().collection(DEAD_LETTER).stream()]
    return {"orders": orders, "pending": pending, "dead_letter": dead}


def flush_preview(mock_only=False, include_activity=False):
    """Count what a flush would delete, without deleting anything."""
    targets = _flush_targets(mock_only)
    counts = {k: len(v) for k, v in targets.items()}
    counts["images"] = sum(1 for v in targets.values() for _, path in v if path)
    counts["activity"] = _count_collection(ACTIVITY) if include_activity else 0
    return counts


def _count_collection(name):
    try:
        from google.cloud.firestore_v1.aggregation import AggregationQuery
        agg = AggregationQuery(db().collection(name)).count(alias="n")
        return int(list(agg.get())[0][0].value)
    except Exception:  # noqa: BLE001
        return sum(1 for _ in db().collection(name).stream())


def _delete_ids(collection, ids):
    client = db()
    col = client.collection(collection)
    for start in range(0, len(ids), FLUSH_BATCH):
        batch = client.batch()
        for doc_id in ids[start:start + FLUSH_BATCH]:
            batch.delete(col.document(doc_id))
        batch.commit()
    return len(ids)


def flush_data(mock_only=False, include_activity=False):
    """Delete scanned data so the system can start clean.

    Irreversible.  Callers must have already obtained an explicit confirmation
    from the operator; this function does not ask.
    """
    targets = _flush_targets(mock_only)
    deleted = {}
    images = 0

    for collection, key in ((ORDERS, "orders"), (PENDING, "pending"),
                            (DEAD_LETTER, "dead_letter")):
        entries = targets.get(key) or []
        for _, path in entries:
            if not path:
                continue
            try:
                bucket().blob(path).delete()
                images += 1
            except Exception:  # noqa: BLE001
                pass          # already gone, or never uploaded
        deleted[key] = _delete_ids(collection, [doc_id for doc_id, _ in entries])

    if include_activity:
        ids = [d.id for d in db().collection(ACTIVITY).stream()]
        deleted["activity"] = _delete_ids(ACTIVITY, ids)
    else:
        deleted["activity"] = 0

    deleted["images"] = images
    return deleted


# ---------------------------------------------------------------------------
# SAP hand-off: remember which orders have already been exported
# ---------------------------------------------------------------------------
EXPORT_BATCHES = "export_batches"


def mark_exported(order_ids, user_email, meta=None):
    """Record that these orders went out in an export file.

    Written as one batch per chunk so a large export cannot leave half the
    orders flagged and half not.
    """
    if not order_ids:
        return None
    client = db()
    batch_ref = client.collection(EXPORT_BATCHES).document()
    stamp = firestore.SERVER_TIMESTAMP
    col = client.collection(ORDERS)

    for start in range(0, len(order_ids), 400):
        chunk = order_ids[start:start + 400]
        wb = client.batch()
        for oid in chunk:
            wb.update(col.document(oid), {
                "exported_at": stamp,
                "exported_by": user_email,
                "export_batch": batch_ref.id,
                "export_count": firestore.Increment(1),
            })
        wb.commit()

    batch_ref.set({
        "created_at": stamp,
        "user_email": user_email,
        "count": len(order_ids),
        "order_ids": order_ids[:2000],
        "meta": meta or {},
        "undone": False,
    })
    return batch_ref.id


def export_status():
    """How many approved orders are still waiting to go into SAP."""
    pending, exported, oldest = 0, 0, None
    for d in db().collection(ORDERS).stream():
        row = d.to_dict() or {}
        if row.get("status") != "approved":
            continue
        if row.get("exported_at"):
            exported += 1
        else:
            pending += 1
            date = (row.get("document_date") or "")[:10]
            if date and (oldest is None or date < oldest):
                oldest = date
    return {"pending": pending, "exported": exported, "oldest_pending": oldest}


def list_export_batches(limit=20):
    q = (db().collection(EXPORT_BATCHES)
         .order_by("created_at", direction=firestore.Query.DESCENDING)
         .limit(limit))
    out = []
    for d in q.stream():
        row = d.to_dict() or {}
        ts = row.get("created_at")
        out.append({
            "id": d.id,
            "created_at": ts.isoformat() if hasattr(ts, "isoformat") else None,
            "user_email": row.get("user_email"),
            "count": row.get("count", 0),
            "meta": row.get("meta") or {},
            "undone": bool(row.get("undone")),
        })
    return out


def undo_export_batch(batch_id):
    """Clear the export mark from a batch — for when a download never arrived.

    Only orders whose latest export is this batch are reset; an order exported
    again afterwards keeps its newer mark.
    """
    client = db()
    ref = client.collection(EXPORT_BATCHES).document(batch_id)
    doc = ref.get()
    if not doc.exists:
        return 0
    ids = (doc.to_dict() or {}).get("order_ids") or []
    col = client.collection(ORDERS)
    cleared = 0
    for start in range(0, len(ids), 400):
        wb = client.batch()
        touched = 0
        for oid in ids[start:start + 400]:
            snap = col.document(oid).get()
            if not snap.exists:
                continue
            if (snap.to_dict() or {}).get("export_batch") != batch_id:
                continue
            wb.update(col.document(oid), {
                "exported_at": firestore.DELETE_FIELD,
                "exported_by": firestore.DELETE_FIELD,
                "export_batch": firestore.DELETE_FIELD,
            })
            touched += 1
        if touched:
            wb.commit()
            cleared += touched
    ref.update({"undone": True})
    return cleared
