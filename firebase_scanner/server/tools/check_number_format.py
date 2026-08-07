"""Report quantities that look like a decimal point read as a thousands separator.

Read-only.  It never writes to Firestore; the output is a list for a human to
judge, because the only proof is the paper form and this script cannot see it.

Usage:
    python tools/check_number_format.py              # summary
    python tools/check_number_format.py --details    # every suspect line
    python tools/check_number_format.py --csv out.csv
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extractor
import firestore_store as store

# A quantity this far from its planned amount is not a normal over-issue.
RATIO_ALARM = 20
RATIO_WATCH = 5


def _ratio(a, b):
    if not a or not b or a <= 0 or b <= 0:
        return None
    return max(a, b) / min(a, b)


def _suspects(order):
    """Lines whose recorded quantity does not survive a sanity check.

    Three decimal places alone proves nothing — plenty of real weights are
    written that way — so it only counts when the value is also far from what
    the form planned for.  Flagging it on its own buried the real cases under
    thousands of ordinary rows.
    """
    out = []
    for ln in order.get("lines") or []:
        qty = extractor._num(ln.get("quantity"))
        plan = extractor._num(ln.get("plan"))
        unit = ln.get("unit") or ""
        if qty is None or qty <= 0:
            continue

        text = f"{qty}"
        three_dp = "." in text and len(text.split(".")[1]) == 3
        ratio = _ratio(qty, plan)
        reasons, severity = [], None

        # Only worth raising if reading it the other way lands nearer the plan.
        alt = extractor._num(text.replace(".", "")) if "." in text else None
        alt_ratio = _ratio(alt, plan) if alt else None
        alt_is_better = bool(ratio and alt_ratio and alt_ratio < ratio)

        if extractor._is_count_unit(unit) and qty != int(qty) and alt_is_better:
            reasons.append(f"หน่วย “{unit}” นับเป็นชิ้น และอ่านเป็นจำนวนเต็มแล้วใกล้แผนกว่า")
            severity = "สูง"
        if ratio and ratio >= RATIO_ALARM:
            reasons.append(f"ห่างจากแผนถึง {ratio:,.0f} เท่า")
            severity = "สูง"
        elif three_dp and ratio and ratio >= RATIO_WATCH:
            reasons.append(f"ทศนิยม 3 ตำแหน่ง และห่างจากแผน {ratio:,.0f} เท่า")
            severity = severity or "ปานกลาง"

        if not reasons:
            continue
        as_thousands = extractor._num(text.replace(".", ""))
        out.append({
            "severity": severity,
            "order_no": order.get("order_no"),
            "document_date": order.get("document_date"),
            "row_no": ln.get("row_no"),
            "item_no": ln.get("item_no"),
            "item_description": ln.get("item_description"),
            "unit": unit,
            "quantity": qty,
            "quantity_raw": ln.get("quantity_raw") or "",
            "plan": plan,
            "would_read_as": as_thousands,
            "reasons": " · ".join(reasons),
        })
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--details", action="store_true", help="แสดงทุกแถวที่น่าสงสัย")
    ap.add_argument("--csv", help="บันทึกผลเป็นไฟล์ CSV")
    args = ap.parse_args()

    orders, _ = store.list_orders(limit=5000)
    rows = [r for o in orders for r in _suspects(o)]

    print(f"ตรวจใบสั่งผลิต {len(orders):,} ใบ")
    print(f"พบแถวที่น่าสงสัย {len(rows):,} แถว "
          f"จาก {len({r['order_no'] for r in rows}):,} ใบ\n")
    if not rows:
        print("ไม่พบรายการที่เข้าข่ายอ่านตัวคั่นผิด")
        return

    high = [r for r in rows if r["severity"] == "สูง"]
    print(f"  ความน่าสงสัยสูง    {len(high):>5} แถว — ควรตรวจก่อน")
    print(f"  ความน่าสงสัยปานกลาง {len(rows)-len(high):>5} แถว")

    if args.details:
        print("\nรายละเอียด")
        for r in sorted(rows, key=lambda x: x["severity"] != "สูง"):
            print(f"\n  [{r['severity']}] ใบ {r['order_no']} ({r['document_date']}) แถว {r['row_no']}")
            print(f"    {r['item_description']} [{r['item_no']}] หน่วย {r['unit']}")
            print(f"    บันทึกไว้ {r['quantity']:,}  ·  ตามแผน {r['plan']}")
            print(f"    ถ้าอ่านเป็นหลักพันจะได้ {r['would_read_as']:,}")
            print(f"    เหตุผล: {r['reasons']}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nบันทึก CSV แล้ว: {args.csv}")

    print("\nสคริปต์นี้ไม่แก้ข้อมูลใด ๆ — ต้องเทียบกับฟอร์มกระดาษก่อนตัดสินใจแก้")


if __name__ == "__main__":
    main()
