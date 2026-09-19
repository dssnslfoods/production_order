"""One-time role rename: supervisor -> approver.

The 2-stage approval workflow (staff -> supervisor) became a 3-stage one
(staff -> reviewer -> approver).  Every user doc whose role is still the old
"supervisor" string needs to become "approver" so they keep exactly the
access level they had before (now DEFAULT_PERMISSIONS["approver"], which is
what DEFAULT_PERMISSIONS["supervisor"] used to be).  Nothing else on the user
doc changes, and no order documents are touched by this script.

Safe by default: without --apply this only prints what it would change.

Usage:
    python tools/migrate_supervisor_to_approver.py               # dry-run (default)
    python tools/migrate_supervisor_to_approver.py --dry-run      # same, explicit
    python tools/migrate_supervisor_to_approver.py --apply        # actually writes
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import firestore_store as store


def find_supervisors():
    """Return the list of user docs (uid + email) whose role == 'supervisor'."""
    out = []
    for d in store.db().collection(store.USERS).where("role", "==", "supervisor").stream():
        row = d.to_dict() or {}
        out.append({"uid": d.id, "email": row.get("email"), "role": row.get("role")})
    return out


def migrate(apply: bool):
    users = find_supervisors()
    if not users:
        print("ไม่พบผู้ใช้ที่มี role = supervisor — ไม่มีอะไรต้องย้าย")
        return 0

    print(f"พบผู้ใช้ {len(users)} คนที่มี role = supervisor:")
    for u in users:
        print(f"  - {u['email']} (uid={u['uid']})")

    if not apply:
        print("\n[DRY RUN] ไม่มีการเขียนข้อมูลจริง — เรียกใหม่พร้อม --apply เพื่อยืนยันการเปลี่ยนแปลง")
        return 0

    print("\nกำลังเปลี่ยน role เป็น approver ...")
    for u in users:
        store.db().collection(store.USERS).document(u["uid"]).update({"role": "approver"})
        print(f"  ✓ {u['email']} -> approver")
    print(f"เสร็จสิ้น: เปลี่ยน role ของผู้ใช้ {len(users)} คน จาก supervisor เป็น approver")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Actually write the changes (default is dry-run).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Explicitly request a dry-run (this is already the default).")
    args = parser.parse_args()

    apply = args.apply and not args.dry_run
    store._init()
    return migrate(apply=apply)


if __name__ == "__main__":
    raise SystemExit(main())
