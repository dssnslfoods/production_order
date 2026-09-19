"""Firebase ID-token verification with role-based access control.

Roles: super_admin, admin, approver, reviewer, staff
- super_admin: full access across all factories, manages factories
- admin: full access within their factory (user management, settings, all operations)
- approver: final approval/return-to-review of reviewed records, view orders, export
- reviewer: confirms staff-submitted scans are correct and sends them to the approver
- staff: scan files, upload to queue, view orders (no approve, no settings)

The first user to log in when no users exist is auto-promoted to super_admin.
"""
import os

from fastapi import Header, HTTPException
from firebase_admin import auth as fb_auth

import firestore_store as store


def _allowed():
    raw = os.environ.get("ALLOWED_EMAILS", "").strip()
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


async def verify_token(authorization: str = Header(default="")):
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="ต้องเข้าสู่ระบบก่อน (missing token)")
    token = authorization.split(" ", 1)[1].strip()
    store._init()
    try:
        decoded = fb_auth.verify_id_token(token)
    except Exception:
        raise HTTPException(status_code=401, detail="โทเคนไม่ถูกต้องหรือหมดอายุ")
    uid = decoded.get("uid")
    email = (decoded.get("email") or "").lower()
    # If user was explicitly created by admin (exists in Firestore), skip ALLOWED_EMAILS
    existing = store.get_user_doc(uid)
    if not existing:
        allow = _allowed()
        if allow and email not in allow:
            raise HTTPException(status_code=403, detail="อีเมลนี้ไม่มีสิทธิ์ใช้งาน")
    info = store.get_user_info(uid, email)
    return {
        "uid": uid,
        "email": decoded.get("email"),
        "role": info["role"],
        "factory_id": info.get("factory_id"),
        "factory_code": info.get("factory_code"),
        "factory_name": info.get("factory_name"),
    }


def require_role(*roles):
    """Factory that returns a FastAPI dependency checking the user has one of the given roles."""
    async def _check(authorization: str = Header(default="")):
        user = await verify_token(authorization)
        if user["role"] not in roles:
            raise HTTPException(status_code=403, detail=f"ต้องมีสิทธิ์ {'/'.join(roles)} เท่านั้น (คุณเป็น {user['role']})")
        return user
    return _check
