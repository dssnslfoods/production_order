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
FACTORIES = "factories"

MAX_RETRY = 3
LOG_RETENTION_DAYS = 90

DEFAULT_PERMISSIONS = {
    "super_admin": ["dashboard", "scan", "orders", "ask", "forecast", "health",
                    "users", "activity", "settings", "approve", "delete",
                    "export", "factories", "confirm_review", "return_to_review",
                    "edit_order"],
    "admin": ["dashboard", "scan", "orders", "ask", "forecast", "health",
              "users", "activity", "settings", "approve", "delete", "export",
              "confirm_review", "return_to_review", "edit_order"],
    "approver": ["dashboard", "scan", "orders", "ask", "forecast", "health",
                 "activity", "approve", "return_to_review", "export"],
    "reviewer": ["dashboard", "scan", "orders", "ask", "forecast", "health",
                 "activity", "confirm_review", "edit_order", "export"],
    "staff": ["dashboard", "scan", "orders", "ask", "forecast", "health",
              "edit_order", "export"],
}

# Bump when a release adds permissions.  Roles saved before that release have
# no opinion about the new keys, so they are granted the default rather than
# silently losing a page that used to be open to everyone.
PERM_VERSION = 4
_NEW_BY_VERSION = {2: ["ask", "forecast", "health"], 3: ["factories"],
                   4: ["confirm_review", "return_to_review", "edit_order"]}

# Users and saved permission tables written before the approver rename still
# say "supervisor"; they are read as approver so nobody is locked out before
# tools/migrate_supervisor_to_approver.py has been run.
LEGACY_ROLE_ALIASES = {"supervisor": "approver"}


def normalize_role(role):
    return LEGACY_ROLE_ALIASES.get(role, role)

DEFAULT_SETTINGS = {
    "provider": "claude",
    "models": {"claude": "claude-opus-4-8", "gemini": "gemini-2.5-flash", "openai": "gpt-4o"},
    "api_keys": {"claude": "", "gemini": "", "openai": ""},
    "drive_folder_id": "",
    # Off by default: a wrong crop deletes columns silently, which costs more
    # than the tokens it saves.
    "auto_crop": False,
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
    for old, new in LEGACY_ROLE_ALIASES.items():
        if old in perms:
            legacy = perms.pop(old)
            perms.setdefault(new, legacy)
    for role in ("super_admin", "admin", "approver", "reviewer", "staff"):
        if role not in perms:
            perms[role] = list(DEFAULT_PERMISSIONS.get(role, []))

    stored_version = int(s.get("perm_version") or 1)
    if stored_version < PERM_VERSION:
        for version, keys in _NEW_BY_VERSION.items():
            if version <= stored_version:
                continue
            for role, granted in perms.items():
                for key in keys:
                    if key in DEFAULT_PERMISSIONS.get(role, []) and key not in granted:
                        granted.append(key)
        db().collection(SETTINGS).document("app").set(
            {"role_permissions": perms, "perm_version": PERM_VERSION}, merge=True)

    # admin/super_admin always keeps users + settings to avoid lockout
    for role in ("super_admin", "admin"):
        for must in ("users", "settings", "dashboard"):
            if must not in perms.get(role, []):
                perms.setdefault(role, []).append(must)
    # super_admin always keeps factories
    if "factories" not in perms.get("super_admin", []):
        perms.setdefault("super_admin", []).append("factories")
    return perms


def save_permissions(perms):
    # admin/super_admin always keeps users + settings
    for role in ("super_admin", "admin"):
        for must in ("users", "settings", "dashboard"):
            if must not in perms.get(role, []):
                perms.setdefault(role, []).append(must)
    if "factories" not in perms.get("super_admin", []):
        perms.setdefault("super_admin", []).append("factories")
    ref = db().collection(SETTINGS).document("app")
    ref.set({"role_permissions": perms, "perm_version": PERM_VERSION}, merge=True)
    # merge=True keeps map keys it was not given, so drop pre-rename role keys.
    ref.update({f"role_permissions.{old}": firestore.DELETE_FIELD
                for old in LEGACY_ROLE_ALIASES})
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
# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------
def add_factory(code, name, created_by):
    doc = {
        "code": code.strip().upper(),
        "name": name.strip(),
        "created_at": firestore.SERVER_TIMESTAMP,
        "created_by": created_by,
    }
    ref = db().collection(FACTORIES).add(doc)[1]
    return ref.id


def list_factories():
    out = []
    for d in db().collection(FACTORIES).order_by("code").stream():
        r = d.to_dict()
        r["id"] = d.id
        ts = r.get("created_at")
        r["created_at"] = ts.isoformat() if hasattr(ts, "isoformat") else None
        out.append(r)
    return out


def get_factory(factory_id):
    if not factory_id:
        return None
    d = db().collection(FACTORIES).document(factory_id).get()
    if not d.exists:
        return None
    r = d.to_dict()
    r["id"] = d.id
    return r


def update_factory(factory_id, patch):
    allowed = {"code", "name"}
    upd = {k: v for k, v in patch.items() if k in allowed}
    if "code" in upd:
        upd["code"] = upd["code"].strip().upper()
    if "name" in upd:
        upd["name"] = upd["name"].strip()
    db().collection(FACTORIES).document(factory_id).update(upd)
    return get_factory(factory_id)


def find_factory_by_code(code):
    code = (code or "").strip().upper()
    if not code:
        return None
    for d in (db().collection(FACTORIES)
              .where("code", "==", code).limit(1).stream()):
        r = d.to_dict()
        r["id"] = d.id
        return r
    return None


def factory_linked_collections(factory_id):
    """Return list of collection names that have docs linked to this factory."""
    linked = []
    for col, label in [(USERS, "ผู้ใช้"), (ORDERS, "ใบเบิก"), (PENDING, "คิวสแกน")]:
        q = db().collection(col).where("factory_id", "==", factory_id).limit(1)
        if any(True for _ in q.stream()):
            linked.append(label)
    return linked


def delete_factory(factory_id):
    db().collection(FACTORIES).document(factory_id).delete()


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
def _review_reasons(data):
    """Fields that being empty means the scan probably lost part of the page."""
    reasons = []
    lines = data.get("lines") or []
    if lines:
        if data.get("plan_total") in (None, ""):
            reasons.append("ไม่พบยอดผลิต Plan")
        if data.get("actual_total") in (None, ""):
            reasons.append("ไม่พบยอดผลิตจริง")
        blank_plan = sum(1 for ln in lines if ln.get("plan") in (None, ""))
        blank_unit = sum(1 for ln in lines if not ln.get("unit"))
        if blank_plan == len(lines):
            reasons.append("ไม่พบคอลัมน์ปริมาณที่ต้องใช้ทั้งใบ")
        if blank_unit == len(lines):
            reasons.append("ไม่พบคอลัมน์หน่วยทั้งใบ")
    # A batch whose expiry precedes its manufacture cannot be right, and the
    # handwritten month digit is where that goes wrong.  Flag the order rather
    # than repairing the date, so someone checks it against the paper.
    bad = sum(1 for b in data.get("batches") or []
              if b.get("mfg_date") and b.get("exp_date")
              and b["exp_date"] <= b["mfg_date"])
    if bad:
        reasons.append(f"วันหมดอายุมาก่อนวันผลิต {bad} batch — ตรวจสอบ MFG/EXP")
    # A requisition is filled in on the day it is used, so a date weeks ahead is
    # almost always DD/MM read as MM/DD (10/09 filed as 9 October).
    doc_date = (data.get("document_date") or "")[:10]
    if doc_date and doc_date > _days_from_today(_FUTURE_DATE_GRACE_DAYS):
        reasons.append(f"วันที่บนเอกสาร {doc_date} อยู่ในอนาคต — วัน/เดือนอาจสลับกัน")
    return reasons


_FUTURE_DATE_GRACE_DAYS = 7


def _days_from_today(days):
    bkk = datetime.timezone(datetime.timedelta(hours=7))
    return (datetime.datetime.now(bkk).date() + datetime.timedelta(days=days)).isoformat()


# Header fields a later sheet may supply: page 1 of a two-page form carries no
# ยอดผลิต and no MFG/EXP block, page 2 carries no product row.  Whichever page
# read a value keeps it — a later page only fills what is still blank.
_MERGEABLE_HEADER = ("document_date", "series_no", "product_name", "product_whse",
                     "plan_total", "actual_total", "plan_unit")


def _pages_of(order):
    """Page numbers already folded into this order (legacy rows imply page 1)."""
    pages = order.get("pages")
    if not pages:
        return [int(order.get("page_no") or 1)]
    out = []
    for p in pages:
        try:
            out.append(int(p))
        except (TypeError, ValueError):
            continue
    return out or [1]


def merge_pages(current, data):
    """Fold one continuation page into the order already stored.

    Returns the Firestore patch, or None when `data` is a page this order has
    already absorbed.  Lines are matched on (ลำดับ, รหัส) as well as on the page
    index, so a misread "Page n of m" cannot duplicate rows that are already in.
    """
    page = int(data.get("page_no") or 1)
    seen = _pages_of(current)
    if page in seen:
        return None

    lines = list(current.get("lines") or [])
    key = lambda ln: (str(ln.get("row_no")), str(ln.get("item_no")))
    have = {key(ln) for ln in lines}
    for ln in data.get("lines") or []:
        if key(ln) not in have:
            have.add(key(ln))
            lines.append(ln)
    lines.sort(key=lambda ln: (ln.get("row_no") is None, ln.get("row_no") or 0))

    batches = list(current.get("batches") or [])
    bkey = lambda b: (b.get("mfg_date"), b.get("exp_date"), b.get("batch_qty"))
    bhave = {bkey(b) for b in batches}
    for b in data.get("batches") or []:
        if bkey(b) not in bhave:
            bhave.add(bkey(b))
            batches.append(b)

    patch = {
        "lines": lines,
        "batches": batches,
        "pages": sorted(seen + [page]),
        "page_total": max(int(current.get("page_total") or 1),
                          int(data.get("page_total") or 1)),
    }
    for f in _MERGEABLE_HEADER:
        if current.get(f) in (None, "") and data.get(f) not in (None, ""):
            patch[f] = data[f]

    # The reasons were judged on a partial form; re-judge on the whole one, or a
    # page-1-only scan stays flagged for a total that page 2 has since supplied.
    reasons = _review_reasons({**current, **patch})
    patch["review_reasons"] = reasons
    patch["needs_review"] = bool(reasons)
    return patch


def page_images_of(row):
    """Every stored sheet image for an order, oldest schema included."""
    imgs = [dict(i) for i in (row.get("source_images") or []) if i.get("path")]
    legacy = row.get("source_image")
    if legacy and not any(i.get("path") == legacy for i in imgs):
        imgs.insert(0, {"page": (_pages_of(row) or [1])[0], "path": legacy})
    return sorted(imgs, key=lambda i: i.get("page") or 0)


def merge_order_page(order_id, data, storage_path=None):
    """Apply merge_pages to a stored order. -> merged | duplicate | locked | missing"""
    ref = db().collection(ORDERS).document(order_id)
    snap = ref.get()
    if not snap.exists:
        return "missing"
    current = snap.to_dict() or {}
    patch = merge_pages(current, data)
    # A page the order already holds is a re-upload, whatever the order's state:
    # there is nothing to add, so it must not land in the failure queue.
    if patch is None:
        return "duplicate"
    # An approved or exported order has been signed off and its source image
    # dropped; quietly rewriting its lines would change a record someone already
    # checked.  Refuse, and let the caller surface the page rather than bin it.
    if current.get("status") in ("approved", "exported"):
        return "locked"
    # Keep the incoming sheet's photo too: every page of the requisition has to
    # stay checkable against the order it was folded into.
    if storage_path:
        imgs = page_images_of(current)
        if not any(i.get("path") == storage_path for i in imgs):
            imgs.append({"page": int(data.get("page_no") or 1), "path": storage_path})
            imgs.sort(key=lambda i: i.get("page") or 0)
        patch["source_images"] = imgs
    patch["merged_at"] = firestore.SERVER_TIMESTAMP
    ref.update(patch)
    return "merged"


# ---------------------------------------------------------------------------
# Parked pages — a multi-page form waits in the queue until every sheet is in
# ---------------------------------------------------------------------------
def hold_page(pid, data):
    """Park a scanned sheet on its queue entry instead of filing a partial order.

    The reading is kept with the file, so completing the set later costs no
    second trip to the vision model.
    """
    db().collection(PENDING).document(pid).update({
        "status": "held",
        "held_order_no": data.get("order_no"),
        "held_page_no": int(data.get("page_no") or 1),
        "held_page_total": int(data.get("page_total") or 1),
        "parsed": data,
        "error": None,
        "held_at": firestore.SERVER_TIMESTAMP,
    })


def list_held_pages(order_no, factory_id=None):
    """Parked sheets for one Order No., lowest page first.

    Filtered in Python on purpose: a single-field equality query needs no
    composite index, so this keeps working without a Firestore migration.
    """
    if not order_no:
        return []
    out = []
    for d in db().collection(PENDING).where("held_order_no", "==", order_no).stream():
        r = d.to_dict() or {}
        if r.get("status") != "held":
            continue
        # Same rule as find_by_order_no: an untagged sheet belongs to no tenant,
        # so it still joins its siblings instead of waiting for a page forever.
        if factory_id and r.get("factory_id") not in (factory_id, None):
            continue
        r["id"] = d.id
        out.append(r)
    return sorted(out, key=lambda r: r.get("held_page_no") or 1)


def assemble_pages(held):
    """Fold parked sheets into one order record. -> (data, page_images)"""
    ordered = sorted(held, key=lambda h: h.get("held_page_no") or 1)
    combined = dict(ordered[0].get("parsed") or {})
    combined["pages"] = [int(ordered[0].get("held_page_no") or 1)]
    for h in ordered[1:]:
        patch = merge_pages(combined, h.get("parsed") or {})
        if patch:
            combined.update(patch)
    images = [{"page": int(h.get("held_page_no") or 1), "path": h["storage_path"]}
              for h in ordered if h.get("storage_path")]
    return combined, images


def add_order(data, source_image, provider, user_email=None, source_filename=None,
              ai_image=None, factory_id=None, page_images=None):
    reasons = _review_reasons(data)
    imgs = list(page_images or [])
    if not imgs and source_image:
        imgs = [{"page": int(data.get("page_no") or 1), "path": source_image}]
    doc = {
        "order_no": data.get("order_no"),
        "document_date": data.get("document_date"),
        "series_no": data.get("series_no"),
        "product_name": data.get("product_name"),
        "product_whse": data.get("product_whse"),
        "plan_total": data.get("plan_total"),
        "actual_total": data.get("actual_total"),
        "plan_unit": data.get("plan_unit"),
        "lines": data.get("lines", []),
        "batches": data.get("batches", []),
        "pages": sorted(data.get("pages") or [int(data.get("page_no") or 1)]),
        "page_total": int(data.get("page_total") or 1),
        "source_image": source_image or (imgs[0]["path"] if imgs else None),
        "source_images": imgs,
        "ai_image": ai_image,
        "source_filename": source_filename,
        "needs_review": bool(reasons),
        "review_reasons": reasons,
        "provider": provider,
        "scanned_by": user_email,
        "scanned_at": firestore.SERVER_TIMESTAMP,
        "status": "draft",
        "factory_id": factory_id,
    }
    ref = db().collection(ORDERS).add(doc)[1]
    return ref.id


RUNS = "scan_runs"


def log_run(trigger, result, factory_id=None):
    """Record one processing run (manual button or scheduled) for the report."""
    doc = {
        "ran_at": firestore.SERVER_TIMESTAMP,
        "trigger": trigger,
        "processed": result.get("processed", 0),
        "succeeded": result.get("succeeded", 0),
        "failed": result.get("failed", 0),
        "skipped": result.get("skipped", 0),
    }
    if factory_id:
        doc["factory_id"] = factory_id
    db().collection(RUNS).add(doc)


def list_runs(limit=20, factory_id=None):
    q = db().collection(RUNS)
    if factory_id:
        q = q.where("factory_id", "==", factory_id)
    q = q.order_by("ran_at", direction=firestore.Query.DESCENDING).limit(limit)
    out = []
    for d in q.stream():
        r = d.to_dict()
        ts = r.get("ran_at")
        r["ran_at"] = ts.isoformat() if hasattr(ts, "isoformat") else None
        out.append(r)
    return out


def count_orders(factory_id=None):
    q = db().collection(ORDERS)
    if factory_id:
        q = q.where("factory_id", "==", factory_id)
    try:
        from google.cloud.firestore_v1.aggregation import AggregationQuery
        agg = AggregationQuery(q).count(alias="n")
        res = agg.get()
        return int(res[0][0].value)
    except Exception:  # noqa: BLE001
        return sum(1 for _ in q.stream())


def list_orders(limit=100, cursor=None, factory_id=None, statuses=None):
    """List orders with cursor-based pagination.

    Returns (orders, next_cursor).  Pass next_cursor back as `cursor`
    to fetch the next page.  next_cursor is None when there are no more.
    With `statuses`, other orders are skipped while still filling the page,
    so a run of hidden orders cannot make a page come back empty.
    """
    q = db().collection(ORDERS)
    if factory_id:
        q = q.where("factory_id", "==", factory_id)
    q = q.order_by("scanned_at", direction=firestore.Query.DESCENDING)
    if cursor:
        snap = db().collection(ORDERS).document(cursor).get()
        if snap.exists:
            q = q.start_after(snap)
    if not statuses:
        q = q.limit(limit + 1)
    picked = []
    for d in q.stream():
        row = d.to_dict()
        if statuses and row.get("status") not in statuses:
            continue
        picked.append((d, row))
        if len(picked) > limit:
            break
    has_more = len(picked) > limit
    picked = picked[:limit]
    out = []
    for d, row in picked:
        row["id"] = d.id
        ts = row.get("scanned_at")
        row["scanned_at"] = ts.isoformat() if hasattr(ts, "isoformat") else None
        out.append(row)
    next_cursor = picked[-1][0].id if has_more else None
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
                "product_whse", "plan_total", "actual_total", "plan_unit",
                "lines", "batches"}
    upd = {k: v for k, v in patch.items() if k in allowed}
    upd["edited"] = True
    upd["edited_at"] = firestore.SERVER_TIMESTAMP
    db().collection(ORDERS).document(order_id).update(upd)
    return get_order(order_id)


def _drop_blobs(paths):
    for p in paths:
        try:
            bucket().blob(p).delete()
        except Exception:  # noqa: BLE001
            pass


def approve_order(order_id, user_email):
    ref = db().collection(ORDERS).document(order_id)
    doc = ref.get()
    # A multi-page order holds one photo per sheet; approving retires them all,
    # or the extra sheets linger in Storage with nothing pointing at them.
    paths = [i["path"] for i in page_images_of(doc.to_dict() or {})] if doc.exists else []
    ref.update({
        "status": "approved",
        "approved_by": user_email,
        "approved_at": firestore.SERVER_TIMESTAMP,
        "source_image": None,
        "source_images": [],
    })
    _drop_blobs(paths)
    return get_order(order_id)


def confirm_review(order_id, user_email):
    """Reviewer confirms a scanned order is correct → moves it to pending_approval."""
    ref = db().collection(ORDERS).document(order_id)
    ref.update({
        "status": "pending_approval",
        "reviewed_by": user_email,
        "reviewed_at": firestore.SERVER_TIMESTAMP,
    })
    return get_order(order_id)


def return_order_to_review(order_id, user_email, reason):
    """Approver bounces an order back to the reviewer with a reason."""
    ref = db().collection(ORDERS).document(order_id)
    ref.update({
        "status": "returned_to_review",
        "rejection_reason": reason,
        "returned_by": user_email,
        "returned_at": firestore.SERVER_TIMESTAMP,
    })
    return get_order(order_id)


def submit_for_review(order_id, user_email):
    """Staff has checked the scanned draft and hands it to the reviewer."""
    ref = db().collection(ORDERS).document(order_id)
    ref.update({
        "status": "pending_review",
        "submitted_by": user_email,
        "submitted_at": firestore.SERVER_TIMESTAMP,
    })
    return get_order(order_id)


def delete_order(order_id):
    ref = db().collection(ORDERS).document(order_id)
    doc = ref.get()
    if doc.exists:
        _drop_blobs([i["path"] for i in page_images_of(doc.to_dict() or {})])
    ref.delete()


# ---------------------------------------------------------------------------
# Pending queue (upload now, scan later — manual button or scheduled)
# ---------------------------------------------------------------------------
def add_pending(raw: bytes, content_type: str, filename: str, user_email=None,
                factory_id=None):
    ext = os.path.splitext(filename or "")[1] or ".jpg"
    path = f"pending/{uuid.uuid4().hex}{ext}"
    bucket().blob(path).upload_from_string(raw, content_type=content_type or "application/octet-stream")
    doc = {
        "filename": filename, "storage_path": path, "content_type": content_type,
        "status": "pending", "error": None, "uploaded_by": user_email,
        "uploaded_at": firestore.SERVER_TIMESTAMP,
    }
    if factory_id:
        doc["factory_id"] = factory_id
    return db().collection(PENDING).add(doc)[1].id


def find_seen_filenames(filenames, factory_id=None):
    """Which of these names were uploaded before. -> {filename: where it is}

    Photos shared through LINE keep their name (S__28327966_0.jpg), so a name
    already on an order or in the queue almost always means the same sheet is
    being sent again.  Only the sheet an order was filed from is on record, so
    this catches most re-uploads rather than all of them.
    """
    names = sorted({n for n in filenames or [] if n})
    seen = {}
    for i in range(0, len(names), 30):  # Firestore caps an "in" filter at 30
        chunk = names[i:i + 30]
        for d in db().collection(ORDERS).where("source_filename", "in", chunk).stream():
            r = d.to_dict() or {}
            if factory_id and r.get("factory_id") != factory_id:
                continue
            seen.setdefault(r.get("source_filename"), f"Order {r.get('order_no') or '-'}")
        for col, label in ((PENDING, "อยู่ในคิวแล้ว"), (DEAD_LETTER, "อยู่ในรายการสแกนล้มเหลว")):
            for d in db().collection(col).where("filename", "in", chunk).stream():
                r = d.to_dict() or {}
                if factory_id and r.get("factory_id") != factory_id:
                    continue
                seen.setdefault(r.get("filename"), label)
    return seen


def get_pending(pid):
    d = db().collection(PENDING).document(pid).get()
    if not d.exists:
        return None
    r = d.to_dict()
    r["id"] = d.id
    return r


def list_pending(factory_id=None):
    q = db().collection(PENDING)
    if factory_id:
        q = q.where("factory_id", "==", factory_id)
    q = q.order_by("uploaded_at")
    out = []
    for d in q.stream():
        r = d.to_dict(); r["id"] = d.id
        ts = r.get("uploaded_at")
        r["uploaded_at"] = ts.isoformat() if hasattr(ts, "isoformat") else None
        out.append(r)
    return out


def download_bytes(blob_path):
    return bucket().blob(blob_path).download_as_bytes()


def delete_pending(pid):
    db().collection(PENDING).document(pid).delete()


def fail_pending(pid, err, permanent=False):
    """Count a failed read; `permanent` skips the retries a re-read cannot fix."""
    ref = db().collection(PENDING).document(pid)
    doc = ref.get()
    data = doc.to_dict() if doc.exists else {}
    retries = data.get("retry_count", 0) + 1
    if permanent or retries >= MAX_RETRY:
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


def list_dead_letter(factory_id=None):
    q = db().collection(DEAD_LETTER)
    if factory_id:
        q = q.where("factory_id", "==", factory_id)
    q = q.order_by("moved_at", direction=firestore.Query.DESCENDING)
    out = []
    for d in q.stream():
        r = d.to_dict()
        r["id"] = d.id
        for ts_field in ("uploaded_at", "moved_at"):
            ts = r.get(ts_field)
            r[ts_field] = ts.isoformat() if hasattr(ts, "isoformat") else None
        out.append(r)
    return out


def get_dead_letter(dlq_id):
    d = db().collection(DEAD_LETTER).document(dlq_id).get()
    if not d.exists:
        return None
    r = d.to_dict()
    r["id"] = d.id
    return r


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
def find_by_order_no(order_no: str, factory_id=None):
    """Return the existing order for this Order No., else None.

    Matched on order_no alone and narrowed in Python.  Filtering inside the
    query silently skipped orders stored with no factory — a super_admin scan
    saves none — and that miss reads as "no duplicate", which is how one
    Production Order got filed twice.  An untagged order belongs to no tenant,
    so it counts as the same document for whoever looks it up; two *tagged*
    factories still never see each other's orders.
    """
    if not order_no:
        return None
    rows = []
    for d in db().collection(ORDERS).where("order_no", "==", order_no).stream():
        r = d.to_dict() or {}
        r["id"] = d.id
        rows.append(r)
    if factory_id:
        rows = [r for r in rows if r.get("factory_id") in (factory_id, None)]
        rows.sort(key=lambda r: r.get("factory_id") != factory_id)  # exact match first
    if not rows:
        return None
    row = rows[0]
    return {"id": row["id"], "order_no": row.get("order_no"),
            "series_no": row.get("series_no"),
            "status": row.get("status"),
            "factory_id": row.get("factory_id"),
            "pages": _pages_of(row),
            "source_filename": row.get("source_filename")}


# ---------------------------------------------------------------------------
# User management (roles: admin, approver, reviewer, staff)
# ---------------------------------------------------------------------------
VALID_ROLES = {"super_admin", "admin", "approver", "reviewer", "staff"}


def get_user_info(uid, email):
    """Get user role + factory_id from Firestore. Auto-creates first user as super_admin."""
    doc = db().collection(USERS).document(uid).get()
    if doc.exists:
        data = doc.to_dict() or {}
        return {
            "role": normalize_role(data.get("role", "staff")),
            "factory_id": data.get("factory_id"),
            "factory_code": data.get("factory_code"),
            "factory_name": data.get("factory_name"),
        }
    if _count_users() == 0:
        _create_user_doc(uid, email, "super_admin", "system")
        return {"role": "super_admin", "factory_id": None,
                "factory_code": None, "factory_name": None}
    _create_user_doc(uid, email, "staff", "auto")
    return {"role": "staff", "factory_id": None,
            "factory_code": None, "factory_name": None}


def get_user_role(uid, email):
    """Backward-compatible wrapper."""
    return get_user_info(uid, email)["role"]


def _count_users():
    try:
        from google.cloud.firestore_v1.aggregation import AggregationQuery
        agg = AggregationQuery(db().collection(USERS)).count(alias="n")
        res = agg.get()
        return int(res[0][0].value)
    except Exception:  # noqa: BLE001
        return sum(1 for _ in db().collection(USERS).limit(1).stream())


def _create_user_doc(uid, email, role, created_by, factory_id=None,
                     factory_code=None, factory_name=None):
    doc = {
        "email": email,
        "role": role,
        "created_at": firestore.SERVER_TIMESTAMP,
        "created_by": created_by,
    }
    if factory_id:
        doc["factory_id"] = factory_id
        doc["factory_code"] = factory_code
        doc["factory_name"] = factory_name
    db().collection(USERS).document(uid).set(doc)


def list_users(factory_id=None):
    q = db().collection(USERS)
    if factory_id:
        q = q.where("factory_id", "==", factory_id)
    q = q.order_by("email")
    out = []
    for d in q.stream():
        r = d.to_dict()
        r["uid"] = d.id
        if "role" in r:
            r["role"] = normalize_role(r["role"])
        ts = r.get("created_at")
        r["created_at"] = ts.isoformat() if hasattr(ts, "isoformat") else None
        out.append(r)
    return out


def update_user_role(uid, role):
    if role not in VALID_ROLES:
        raise ValueError(f"role ต้องเป็น {VALID_ROLES}")
    db().collection(USERS).document(uid).update({"role": role})


def update_user_factory(uid, factory_id, factory_code, factory_name):
    db().collection(USERS).document(uid).update({
        "factory_id": factory_id,
        "factory_code": factory_code,
        "factory_name": factory_name,
    })


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
def log_activity(action, user_email, user_role, detail=None, target_id=None,
                 factory_id=None):
    doc = {
        "action": action,
        "user_email": user_email,
        "user_role": user_role,
        "detail": detail,
        "target_id": target_id,
        "timestamp": firestore.SERVER_TIMESTAMP,
    }
    if factory_id:
        doc["factory_id"] = factory_id
    db().collection(ACTIVITY).add(doc)


def list_activity(limit=100, factory_id=None):
    q = db().collection(ACTIVITY)
    if factory_id:
        q = q.where("factory_id", "==", factory_id)
    q = q.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(limit)
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
    # Each entry is (doc_id, [blob paths]) — an order may hold one photo per
    # sheet, and every one of them has to go with the document.
    orders = []
    for d in db().collection(ORDERS).stream():
        row = d.to_dict() or {}
        if mock_only and not row.get("is_mock"):
            continue
        orders.append((d.id, [i["path"] for i in page_images_of(row)]))

    if mock_only:
        # Only scanned orders carry the mock tag; leave real queue items alone.
        return {"orders": orders, "pending": [], "dead_letter": []}

    pending = [(d.id, [p for p in [(d.to_dict() or {}).get("storage_path")] if p])
               for d in db().collection(PENDING).stream()]
    dead = [(d.id, [p for p in [(d.to_dict() or {}).get("storage_path")] if p])
            for d in db().collection(DEAD_LETTER).stream()]
    return {"orders": orders, "pending": pending, "dead_letter": dead}


def flush_preview(mock_only=False, include_activity=False):
    """Count what a flush would delete, without deleting anything."""
    targets = _flush_targets(mock_only)
    counts = {k: len(v) for k, v in targets.items()}
    counts["images"] = sum(len(paths) for v in targets.values() for _, paths in v)
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
        for _, paths in entries:
            for path in paths:
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


def mark_exported(order_ids, user_email, meta=None, factory_id=None):
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

    doc = {
        "created_at": stamp,
        "user_email": user_email,
        "count": len(order_ids),
        "order_ids": order_ids[:2000],
        "meta": meta or {},
        "undone": False,
    }
    if factory_id:
        doc["factory_id"] = factory_id
    batch_ref.set(doc)
    return batch_ref.id


def export_status(factory_id=None, approved_by=None):
    """How many approved orders are still waiting to go into SAP."""
    q = db().collection(ORDERS)
    if factory_id:
        q = q.where("factory_id", "==", factory_id)
    pending, exported, oldest = 0, 0, None
    for d in q.stream():
        row = d.to_dict() or {}
        if row.get("status") != "approved":
            continue
        if approved_by and row.get("approved_by") != approved_by:
            continue
        if row.get("exported_at"):
            exported += 1
        else:
            pending += 1
            date = (row.get("document_date") or "")[:10]
            if date and (oldest is None or date < oldest):
                oldest = date
    return {"pending": pending, "exported": exported, "oldest_pending": oldest}


def list_export_batches(limit=20, factory_id=None):
    q = db().collection(EXPORT_BATCHES)
    if factory_id:
        q = q.where("factory_id", "==", factory_id)
    q = q.order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit)
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
