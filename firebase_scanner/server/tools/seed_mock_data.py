"""Generate realistic mock production orders for forecasting development.

The generator models real factory behaviour rather than random noise: each
product has an implied bill of materials, and every line's quantity is derived
from the order's production volume times that ratio, plus a small yield
variance.  Forecasting features can therefore be validated against a known
ground truth (the BOM ratios printed by --show-bom).

Every document is tagged `is_mock: True` so mock rows stay identifiable.
This script only ever writes; it never deletes or modifies existing data.

Usage:
    python tools/seed_mock_data.py                  # dry run, prints a summary
    python tools/seed_mock_data.py --preview 3      # dry run + sample documents
    python tools/seed_mock_data.py --show-bom       # print the ground-truth BOM
    python tools/seed_mock_data.py --write          # write to Firestore
"""
import argparse
import datetime as dt
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SEED = 20260804
START_DATE = dt.date(2026, 2, 2)
END_DATE = dt.date(2026, 8, 3)
MONTHLY_GROWTH = 0.03          # production volume trend per month
BASE_VOLUME_KG = 420.0
APPROVAL_LAG_DAYS = 14         # anything older than this is fully approved

SCANNERS = [
    "somchai.p@nslfoods.com",
    "nattaya.k@nslfoods.com",
    "wichai.s@nslfoods.com",
]
APPROVERS = ["supervisor@nslfoods.com", "dssnslfoods@gmail.com"]
PROVIDERS = ["claude", "claude", "claude", "gemini"]

# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------
# item_no -> (description, unit, warehouse)
MATERIALS = {
    "10101001": ("แป้งสาลีอเนกประสงค์ ตราว่าว", "KG", "P8-RM01"),
    "10101005": ("แป้งสาลีโปรตีนสูง ตราหงส์", "KG", "P8-RM01"),
    "10202004": ("น้ำมันถั่วเหลือง ตรา MEI (Lamsoon)", "KG", "P8-PD05"),
    "10203001": ("เนยสดจืด ตราออร์คิด", "KG", "P8-CH01"),
    "10301002": ("น้ำตาลทรายขาวบริสุทธิ์", "KG", "P8-RM02"),
    "10401003": ("นมผงขาดมันเนย", "KG", "P8-RM02"),
    "10402001": ("ไข่ไก่เหลวพาสเจอร์ไรส์", "KG", "P8-CH01"),
    "10501006": ("เกลือป่นบริโภค", "KG", "P8-RM02"),
    "10502004": ("ยีสต์แห้งสำเร็จรูป", "KG", "P8-RM02"),
    "10601002": ("หมูหยองปรุงรส", "KG", "P8-CH02"),
    "10601007": ("ทูน่าในน้ำเกลือ", "KG", "P8-CH02"),
    "10602003": ("แฮมสไลซ์", "KG", "P8-CH02"),
    "10602009": ("ชีสแผ่นเชดดาร์", "KG", "P8-CH02"),
    "10603001": ("เนื้อหมูบด", "KG", "P8-CH02"),
    "10701004": ("น้ำพริกเผา", "KG", "P8-RM03"),
    "10702002": ("มายองเนส", "KG", "P8-RM03"),
    "10703005": ("ซอสเทอริยากิ", "KG", "P8-RM03"),
    "10801003": ("ครีมวานิลลา", "KG", "P8-CH01"),
    "10901002": ("ถุงบรรจุ PE ลายบริษัท", "KG", "P8-PK01"),
    "10901007": ("ฉลากสินค้า", "KG", "P8-PK01"),
}

RESOURCES = {
    "P8-MC-E001": ("เครื่องผสมแป้ง Spiral Mixer", "Hour", "P8-PD05"),
    "P8-MC-E004": ("เตาอบสายพาน Tunnel Oven", "Hour", "P8-PD05"),
    "DL-217": ("แรงงานฝ่ายผลิต", "Hour", "P8-PD05"),
    "DL-305": ("แรงงานฝ่ายบรรจุ", "Hour", "P8-PD05"),
}

# Consumption per 1 KG of finished product, shared by every bread-based SKU.
BASE_BOM = {
    "10101001": 0.3150,
    "10202004": 0.0061,
    "10203001": 0.0420,
    "10301002": 0.0380,
    "10401003": 0.0125,
    "10402001": 0.0290,
    "10501006": 0.0048,
    "10502004": 0.0036,
    "10901002": 0.0090,
    "10901007": 0.0045,
}

RESOURCE_BOM = {
    "P8-MC-E001": 0.0021,
    "P8-MC-E004": 0.0035,
    "DL-217": 0.0085,
    "DL-305": 0.0052,
}

# Labour intensity per KG differs by product: hand-assembled sandwiches take
# far more work per kilogram than plain filled bread.  Without this the hours
# a day takes would be a fixed multiple of its kilograms, and "which day is
# busiest" would collapse back into "which day is heaviest".
RESOURCE_FACTOR = {
    "7010101004": 1.35,      # หมูหยองน้ำพริกเผา — โรยและทาด้วยมือ
    "7010101007": 1.20,      # ทูน่าสลัด — ผสมไส้ก่อน
    "7010401001": 1.45,      # เดนิชคาโบว์นาร่า — ประกอบหลายชั้น
    "7010201003": 1.10,      # แฮมชีส — วางแผ่นเดียว
    "7010301002": 0.70,      # ขนมปังไส้ครีม — บีบไส้ด้วยเครื่อง
    "7010501005": 1.55,      # เบอร์เกอร์เทอริยากิ — ขึ้นรูปและย่าง
}

# series_no -> (product_name, extra BOM, relative production share)
PRODUCTS = {
    "7010101004": ("แซนวิชหมูหยองน้ำพริกเผา",
                   {"10601002": 0.1150, "10701004": 0.0850, "10702002": 0.0620}, 0.26),
    "7010101007": ("แซนวิชทูน่าสลัด",
                   {"10601007": 0.1480, "10702002": 0.0950}, 0.19),
    "7010401001": ("แซนวิชเดนิชคาโบว์นาร่า",
                   {"10602003": 0.0920, "10602009": 0.0780, "10101005": 0.0850}, 0.15),
    "7010201003": ("แซนวิชแฮมชีส",
                   {"10602003": 0.1180, "10602009": 0.0960, "10702002": 0.0410}, 0.17),
    "7010301002": ("ขนมปังไส้ครีมวานิลลา",
                   {"10801003": 0.1850}, 0.13),
    "7010501005": ("เบอร์เกอร์หมูเทอริยากิ",
                   {"10603001": 0.1650, "10703005": 0.0720}, 0.10),
}

# Weekday production intensity (Mon=0 .. Sun=6).  Sunday is a shutdown day.
WEEKDAY_FACTOR = [1.10, 1.05, 1.00, 1.05, 1.12, 0.62, 0.0]


def _bom_for(series_no):
    """Full ground-truth BOM for a product: base bread + filling + resources."""
    _, extra, _ = PRODUCTS[series_no]
    return {**BASE_BOM, **extra}


def _orders_for_day(rng, day):
    """How many production orders run on a given date."""
    if WEEKDAY_FACTOR[day.weekday()] == 0.0:
        return 0
    if day.weekday() == 5:                      # Saturday: short shift
        return rng.choice([1, 1, 2])
    return rng.choice([2, 3, 3, 4])


def _volume(rng, day):
    """Planned production volume in KG, with trend, weekly and month-end effects."""
    months = (day.year - START_DATE.year) * 12 + (day.month - START_DATE.month)
    trend = (1 + MONTHLY_GROWTH) ** months
    month_end = 1.15 if day.day >= 26 else 1.0
    noise = rng.uniform(0.82, 1.18)
    return BASE_VOLUME_KG * trend * WEEKDAY_FACTOR[day.weekday()] * month_end * noise


def _round(value, unit):
    return round(value, 2) if unit == "Hour" else round(value, 3)


def _build_lines(rng, series_no, plan_total):
    """Turn the BOM into scanned line items: plan is exact, quantity has variance."""
    lines = []
    row = 1
    for item_no, ratio in _bom_for(series_no).items():
        desc, unit, whse = MATERIALS[item_no]
        plan = plan_total * ratio
        # Real yield variance: usually slightly over plan, occasionally under.
        actual = plan * rng.normalvariate(1.018, 0.035)
        lines.append({
            "row_no": row,
            "item_no": item_no,
            "item_description": desc,
            "type": "Item",
            "quantity": _round(max(actual, 0.0), unit),
            "whse": whse,
            "plan": _round(plan, unit),
            "unit": unit,
        })
        row += 1
    labour = RESOURCE_FACTOR.get(series_no, 1.0)
    for item_no, ratio in RESOURCE_BOM.items():
        desc, unit, whse = RESOURCES[item_no]
        plan = plan_total * ratio * labour
        actual = plan * rng.normalvariate(1.005, 0.06)
        lines.append({
            "row_no": row,
            "item_no": item_no,
            "item_description": desc,
            "type": "Resource",
            "quantity": _round(max(actual, 0.0), unit),
            "whse": whse,
            "plan": _round(plan, unit),
            "unit": unit,
        })
        row += 1
    return lines


def _status_for(rng, day, today):
    age = (today - day).days
    if age > APPROVAL_LAG_DAYS:
        return "approved"
    return rng.choices(["approved", "draft", "pending_approval"],
                       weights=[70, 20, 10])[0]


def generate(today=None):
    """Build the full mock dataset as a list of Firestore-shaped documents."""
    today = today or END_DATE
    rng = random.Random(SEED)
    series_ids = list(PRODUCTS)
    weights = [PRODUCTS[s][2] for s in series_ids]

    docs = []
    seq_by_month = {}
    day = START_DATE
    while day <= END_DATE:
        for _ in range(_orders_for_day(rng, day)):
            series_no = rng.choices(series_ids, weights=weights)[0]
            product_name = PRODUCTS[series_no][0]
            plan_total = round(_volume(rng, day), 3)
            # Actual output lands close to plan but rarely exactly on it.
            actual_total = round(plan_total * rng.normalvariate(0.995, 0.018), 1)

            key = (day.year, day.month)
            seq_by_month[key] = seq_by_month.get(key, 0) + 1
            order_no = f"3{day.strftime('%y%m')}{seq_by_month[key]:04d}"

            status = _status_for(rng, day, today)
            scanned_at = dt.datetime.combine(
                day, dt.time(rng.randint(8, 17), rng.randint(0, 59))
            )
            doc = {
                "order_no": order_no,
                "document_date": day.isoformat(),
                "series_no": series_no,
                "product_name": product_name,
                "plan_total": plan_total,
                "actual_total": actual_total,
                "plan_unit": "KG",
                "lines": _build_lines(rng, series_no, plan_total),
                "source_image": None,
                "source_filename": f"MOCK-{order_no}.jpg",
                "provider": rng.choice(PROVIDERS),
                "scanned_by": rng.choice(SCANNERS),
                "scanned_at": scanned_at,
                "status": status,
                "is_mock": True,
            }
            if status == "approved":
                doc["approved_by"] = rng.choice(APPROVERS)
                doc["approved_at"] = scanned_at + dt.timedelta(
                    hours=rng.randint(2, 48)
                )
            docs.append(doc)
        day += dt.timedelta(days=1)
    return docs


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def summarize(docs):
    by_month, by_status, usage = {}, {}, {}
    for d in docs:
        month = d["document_date"][:7]
        by_month[month] = by_month.get(month, 0) + 1
        by_status[d["status"]] = by_status.get(d["status"], 0) + 1
        for ln in d["lines"]:
            if ln["type"] == "Item":
                usage[ln["item_no"]] = usage.get(ln["item_no"], 0.0) + ln["quantity"]

    print(f"เอกสารทั้งหมด : {len(docs)} ใบ")
    print(f"ช่วงวันที่     : {docs[0]['document_date']} → {docs[-1]['document_date']}")
    print(f"สถานะ         : " + ", ".join(f"{k}={v}" for k, v in by_status.items()))
    print("\nจำนวนใบเบิกต่อเดือน")
    for month, n in sorted(by_month.items()):
        print(f"  {month}  {n:>4} ใบ  {'█' * (n // 4)}")
    print("\nยอดใช้วัตถุดิบรวม 10 อันดับแรก (KG)")
    top = sorted(usage.items(), key=lambda kv: -kv[1])[:10]
    for item_no, qty in top:
        print(f"  {item_no}  {MATERIALS[item_no][0]:<38} {qty:>12,.1f}")


def show_bom():
    print("Ground truth BOM — สัดส่วนการใช้ต่อการผลิต 1 KG")
    print("(ใช้ตรวจว่าโมเดลพยากรณ์คำนวณกลับมาได้ตรงหรือไม่)\n")
    for series_no, (name, extra, share) in PRODUCTS.items():
        print(f"{series_no}  {name}   (สัดส่วนการผลิต {share:.0%})")
        for item_no, ratio in _bom_for(series_no).items():
            print(f"    {item_no}  {MATERIALS[item_no][0]:<38} {ratio:.4f}")
        print()
    print("Resource (ทุกผลิตภัณฑ์)")
    for item_no, ratio in RESOURCE_BOM.items():
        print(f"    {item_no:<12} {RESOURCES[item_no][0]:<34} {ratio:.4f} Hour")


def write_to_firestore(docs, batch_size=200):
    import firestore_store as store

    db = store.db()
    col = db.collection(store.ORDERS)
    written = 0
    for start in range(0, len(docs), batch_size):
        chunk = docs[start:start + batch_size]
        batch = db.batch()
        for doc in chunk:
            batch.set(col.document(), doc)
        batch.commit()
        written += len(chunk)
        print(f"  เขียนแล้ว {written}/{len(docs)}")
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="เขียนลง Firestore จริง (ค่าเริ่มต้นคือ dry run)")
    ap.add_argument("--preview", type=int, default=0,
                    help="แสดงตัวอย่างเอกสาร N ใบเป็น JSON")
    ap.add_argument("--show-bom", action="store_true",
                    help="แสดงสูตร BOM ที่ใช้สร้างข้อมูล")
    args = ap.parse_args()

    if args.show_bom:
        show_bom()
        return

    docs = generate()
    summarize(docs)

    if args.preview:
        print(f"\n--- ตัวอย่าง {args.preview} ใบ ---")
        sample = docs[:args.preview]
        print(json.dumps(sample, ensure_ascii=False, indent=2, default=str))

    if not args.write:
        print("\n[DRY RUN] ยังไม่ได้เขียนลง Firestore — เพิ่ม --write เพื่อเขียนจริง")
        return

    print(f"\nกำลังเขียน {len(docs)} เอกสารลง Firestore...")
    n = write_to_firestore(docs)
    print(f"เสร็จสิ้น: เขียน {n} เอกสาร (ทุกใบมี is_mock=True)")


if __name__ == "__main__":
    main()
