"""Natural-language questions over production data.

The model is used twice and never in between: once to turn a Thai question
into a query spec, and once to phrase the computed result as a sentence.  All
arithmetic happens in analytics.py.  This split exists because a model asked
to both read data and total it will occasionally return a confident, wrong
number — and a wrong material total is indistinguishable from a right one to
the person reading it.
"""
import datetime as dt
import json
import re
import time

import analytics
import extractor

ANSWER_CACHE_TTL = 300
_answer_cache = {}

SUGGESTIONS = [
    "เดือนที่แล้วใช้แป้งสาลีไปเท่าไหร่",
    "วัตถุดิบ 10 อันดับที่ใช้เยอะที่สุดในไตรมาสนี้",
    "เทียบการใช้น้ำตาลทรายเดือนนี้กับเดือนที่แล้ว",
    "รายการไหนเบิกเกินแผนมากที่สุด",
    "แนวโน้มการใช้มายองเนส 6 เดือนที่ผ่านมา",
    "เดือนหน้าต้องเตรียมวัตถุดิบอะไรบ้าง",
]

_PLAN_PROMPT = """คุณคือตัวแปลงคำถามเป็นคำสั่งค้นข้อมูลของระบบใบเบิกวัตถุดิบโรงงาน
วันนี้คือ {today}

ข้อมูลที่มี: ใบสั่งผลิต แต่ละใบมี ยอดผลิต (plan_total, หน่วย KG)
และรายการวัตถุดิบที่เบิก แต่ละรายการมี ปริมาณจริง (quantity) กับ ปริมาณตามแผน (plan)

วัตถุดิบที่มีในระบบ:
{materials}

ผลิตภัณฑ์ที่มีในระบบ:
{products}

ช่วงข้อมูลที่มี: {date_min} ถึง {date_max}

แปลงคำถามของผู้ใช้เป็น JSON ตามรูปแบบนี้ (ตอบ JSON ล้วน ไม่มีคำอธิบาย ไม่มี markdown):
{{
  "intent": "aggregate" | "forecast" | "unsupported",
  "metric": "quantity" | "plan" | "variance" | "order_count" | "production",
  "group_by": "none" | "material" | "product" | "month" | "warehouse",
  "filters": {{
    "material": "ชื่อวัตถุดิบที่ถาม หรือ null",
    "product": "ชื่อผลิตภัณฑ์ที่ถาม หรือ null",
    "status": "all" | "approved" | "draft" | "pending_review" | "pending_approval" | "returned_to_review",
    "date_from": "YYYY-MM-DD หรือ null",
    "date_to": "YYYY-MM-DD หรือ null"
  }},
  "compare_to": {{"date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD"}} หรือ null,
  "limit": จำนวนแถวสูงสุด (ค่าเริ่มต้น 10)
}}

ความหมายของ metric:
- quantity   = ปริมาณที่เบิกจริง (ใช้เป็นค่าเริ่มต้นเมื่อถามว่า "ใช้ไปเท่าไหร่")
- plan       = ปริมาณตามแผน
- variance   = ผลต่าง เบิกจริง ลบ แผน (ใช้เมื่อถามเรื่องเบิกเกิน/ขาดแผน)
- order_count= จำนวนใบสั่งผลิต
- production = ยอดผลิตรวม

กติกา:
- ถามถึง "แนวโน้ม" หรือ "ย้อนหลัง N เดือน" ให้ใช้ group_by = "month"
- ถามหา "อันดับ" หรือ "เยอะสุด" ให้ใช้ group_by = "material" (หรือ "product" ถ้าถามถึงสินค้า)
- ถามเปรียบเทียบสองช่วงเวลา ให้ใส่ compare_to
- ถามถึงอนาคต การพยากรณ์ หรือการเตรียมของ ให้ตอบ intent = "forecast" เท่านั้น
- คำถามที่ไม่เกี่ยวกับข้อมูลใบสั่งผลิต ให้ตอบ intent = "unsupported"
- status ให้ใช้ "all" เว้นแต่ผู้ใช้ถามถึงสถานะโดยตรง

คำถาม: {question}"""

_NARRATE_PROMPT = """คุณคือ “น้อง Order” ผู้ช่วยวิเคราะห์ข้อมูลวัตถุดิบของโรงงาน
บุคลิก: เป็นกันเอง สดใส เรียกตัวเองว่า “น้อง Order” ลงท้ายด้วย “ค่ะ” แต่ทำงานแม่นยำแบบมืออาชีพ
ไม่เล่นมุกจนเสียสาระ และไม่ประจบ

คำถามของผู้ใช้: {question}

ผลการคำนวณจากฐานข้อมูลจริง (ระบบคำนวณมาให้แล้ว):
{result}

เขียนคำตอบภาษาไทย 1-3 ประโยค
กติกาเด็ดขาด:
- ใช้เฉพาะตัวเลขที่ให้มาข้างบนเท่านั้น ห้ามคำนวณเพิ่ม ห้ามประมาณ ห้ามเดา
- ใส่หน่วยกำกับตัวเลขเสมอ
- ถ้ามีการเปรียบเทียบ ให้บอกว่าเพิ่มขึ้นหรือลดลงกี่เปอร์เซ็นต์
- ถ้าตัวเลขมีนัยที่ควรรู้ ให้เสริมข้อสังเกตสั้น ๆ อย่างมืออาชีพ เช่น ควรเผื่อของ
  หรือควรตรวจสอบ แต่ห้ามสร้างสาเหตุที่ข้อมูลไม่ได้บอก
- ไม่ต้องทักทายซ้ำ ไม่ต้องสรุปซ้ำท้ายประโยค
- ห้ามใส่ตาราง (ระบบแสดงตารางให้อยู่แล้ว)
- ใช้ emoji ได้ไม่เกิน 1 ตัว และเฉพาะเมื่อช่วยสื่อความ"""


def _catalog_text(mapping, limit=60):
    return "\n".join(f"- {k} {v}" for k, v in list(mapping.items())[:limit])


def _date_range(orders):
    dates = sorted((o.get("document_date") or "")[:10] for o in orders
                   if o.get("document_date"))
    return (dates[0], dates[-1]) if dates else ("-", "-")


def _plan(question, orders, provider, api_key, model):
    cleaned, _ = analytics.clean_orders(orders)
    materials, products = analytics.catalog(cleaned)
    date_min, date_max = _date_range(cleaned)
    prompt = _PLAN_PROMPT.format(
        today=dt.date.today().isoformat(),
        materials=_catalog_text(materials),
        products=_catalog_text(products),
        date_min=date_min, date_max=date_max,
        question=question,
    )
    raw = extractor.chat(prompt, provider, api_key, model, max_tokens=800)
    return extractor.parse_json(raw)


def _forecast_digest(f, top=8):
    """Compact the forecast into something small enough to narrate over."""
    if not f.get("ready"):
        return {"ready": False, "reason": f.get("reason")}
    return {
        "เดือนที่พยากรณ์": f["target_months"],
        "ยอดผลิตเดือนล่าสุด": f["production_total_last_month"],
        "ยอดผลิตที่พยากรณ์": f["production_total_forecast"],
        "วัตถุดิบที่ต้องเตรียม": [
            {"ชื่อ": m["item_description"], "พยากรณ์": m["forecast"],
             "หน่วย": m["unit"], "เฉลี่ยต่อเดือน": m["avg_monthly"],
             "ความเชื่อมั่น": m["confidence"]}
            for m in f["materials"][:top]
        ],
    }


def _result_digest(result, spec):
    """Trim the computed result to the few facts the narration may restate."""
    digest = {
        "metric": spec.get("metric"),
        "หน่วย": result.get("unit"),
        "ยอดรวม": result.get("total"),
        "จำนวนใบที่เกี่ยวข้อง": result.get("n_orders"),
        "แถว": [{"รายการ": r["label"], "ค่า": r["value"], "หน่วย": r["unit"]}
                for r in result.get("rows", [])[:12]],
    }
    if result.get("comparison"):
        c = result["comparison"]
        digest["เปรียบเทียบ"] = {
            "ช่วงที่นำมาเทียบ": f"{c.get('date_from')} ถึง {c.get('date_to')}",
            "ยอดของช่วงเทียบ": c["total"],
            "ผลต่าง": c["delta"],
            "เปลี่ยนแปลงร้อยละ": c["delta_pct"],
            "จำนวนวันที่มีการผลิตช่วงนี้": result.get("active_days"),
            "จำนวนวันที่มีการผลิตช่วงเทียบ": c.get("active_days"),
            "เฉลี่ยต่อวันช่วงนี้": c.get("per_day_now"),
            "เฉลี่ยต่อวันช่วงเทียบ": c.get("per_day"),
            "เปลี่ยนแปลงร้อยละต่อวัน": c.get("per_day_delta_pct"),
        }
        if c.get("length_mismatch"):
            digest["คำเตือนสำคัญ"] = (
                "สองช่วงเวลามีจำนวนวันผลิตไม่เท่ากัน ยอดรวมจึงเทียบกันตรง ๆ ไม่ได้ "
                "ให้ตอบโดยใช้ค่าเฉลี่ยต่อวันเป็นหลัก และบอกผู้ใช้ด้วยว่าช่วงปัจจุบัน "
                "ยังไม่ครบเดือน ห้ามบอกว่าลดลงหรือเพิ่มขึ้นตามยอดรวม")
    if result.get("excluded_outliers"):
        digest["หมายเหตุ"] = (f"ตัดใบที่ยอดผลิตผิดปกติออก "
                             f"{result['excluded_outliers']} ใบ")
    return digest


def _narrate(question, digest, provider, api_key, model):
    prompt = _NARRATE_PROMPT.format(
        question=question,
        result=json.dumps(digest, ensure_ascii=False, indent=2),
    )
    text = extractor.chat(prompt, provider, api_key, model, max_tokens=500)
    return (text or "").strip()


def _cache_key(question):
    return re.sub(r"\s+", " ", (question or "").strip().lower())


def ask(question, provider, api_key, model):
    """Answer one question. Returns the narrated text plus the raw computation."""
    question = (question or "").strip()
    if not question:
        raise ValueError("กรุณาพิมพ์คำถาม")
    if len(question) > 500:
        raise ValueError("คำถามยาวเกินไป กรุณาถามให้สั้นลง")

    key = _cache_key(question)
    hit = _answer_cache.get(key)
    if hit and time.time() - hit["at"] < ANSWER_CACHE_TTL:
        return {**hit["payload"], "cached": True}

    orders = analytics.load_orders()
    if not orders:
        return {"answer": "ยังไม่มีข้อมูลใบสั่งผลิตในระบบเลยค่ะ ลองสแกนเข้ามาก่อนนะคะ", "kind": "empty",
                "rows": [], "cached": False}

    spec = _plan(question, orders, provider, api_key, model)
    intent = (spec.get("intent") or "aggregate").lower()

    if intent == "unsupported":
        payload = {"answer": "คำถามนี้อยู่นอกขอบเขตข้อมูลใบสั่งผลิตค่ะ "
                             "ลองถามน้องเรื่องปริมาณการใช้วัตถุดิบ ยอดผลิต "
                             "หรือเปรียบเทียบระหว่างช่วงเวลาดูนะคะ",
                   "kind": "unsupported", "rows": [], "spec": spec, "cached": False}
        _answer_cache[key] = {"at": time.time(), "payload": payload}
        return payload

    if intent == "forecast":
        f = analytics.forecast(orders)
        answer = _narrate(question, _forecast_digest(f), provider, api_key, model)
        payload = {"answer": answer, "kind": "forecast", "spec": spec,
                   "rows": [{"label": m["item_description"],
                             "value": m["forecast"], "unit": m["unit"],
                             "confidence": m["confidence"]}
                            for m in f.get("materials", [])[:12]],
                   "forecast_ready": f.get("ready", False), "cached": False}
        _answer_cache[key] = {"at": time.time(), "payload": payload}
        return payload

    result = analytics.run_with_comparison(spec, orders)

    if result.get("not_found"):
        payload = {"answer": f"น้องหา \"{result['not_found']}\" ในข้อมูลไม่เจอค่ะ "
                             "ลองตรวจสอบชื่อวัตถุดิบหรือผลิตภัณฑ์อีกครั้งนะคะ",
                   "kind": "not_found", "rows": [], "spec": spec, "cached": False}
        _answer_cache[key] = {"at": time.time(), "payload": payload}
        return payload

    answer = _narrate(question, _result_digest(result, spec), provider, api_key, model)
    payload = {
        "answer": answer,
        "kind": "aggregate",
        "spec": spec,
        "rows": result["rows"],
        "total": result["total"],
        "unit": result["unit"],
        "n_orders": result["n_orders"],
        "comparison": result.get("comparison"),
        "excluded_outliers": result.get("excluded_outliers", 0),
        "chart": (spec.get("group_by") == "month"),
        "cached": False,
    }
    _answer_cache[key] = {"at": time.time(), "payload": payload}
    return payload
