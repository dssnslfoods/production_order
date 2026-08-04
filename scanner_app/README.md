# ระบบสแกนใบเบิกวัตถุดิบ → Excel

เว็บแอปสำหรับสแกนรูป/PDF ใบเบิกวัตถุดิบ (เขียนมือ) แล้วดึงข้อมูลด้วย AI Vision
ไปบันทึกต่อท้ายไฟล์ `ใบเบิกวัตถุดิบ.xlsx` โดยอัตโนมัติ

## ความสามารถ
- 📤 วางไฟล์เข้า folder `data/inbox/` (ผ่านหน้าเว็บ ลาก-วาง หรือก๊อปปี้ไฟล์ตรงๆ)
- ▶︎ ปุ่ม **"สแกนทันที"** ประมวลผลไฟล์ทั้งหมดใน inbox
- ⏰ **สแกนอัตโนมัติแบบตั้งเวลา** (ทุกๆ N นาที) เปิด/ปิดได้จากหน้าเว็บ
- 🤖 เลือก AI ได้ 3 เจ้า: **Claude / Gemini / GPT** (ตั้งค่า API key ในหน้าเว็บ)
- 📊 บันทึกลง 3 sheet: `production_actual`, `production_workforce`, `production_pack`
  และเก็บประวัติทุกครั้งใน sheet `scan_log`
- 📁 ไฟล์ที่สแกนสำเร็จย้ายไป `data/scanned/` · ไฟล์ที่ล้มเหลวไป `data/failed/`

## วิธีเริ่มใช้งาน

```bash
cd scanner_app
./run.sh          # ครั้งแรกจะสร้าง venv + ติดตั้ง dependency ให้อัตโนมัติ
```
เปิดเบราว์เซอร์ที่ **http://127.0.0.1:8000**

### ตั้งค่าครั้งแรก (ในหน้าเว็บ)
1. เลือก **ผู้ให้บริการ AI** (Claude แนะนำสำหรับลายมือไทย)
2. วาง **API Key** — ขอได้จาก:
   - Claude: https://console.anthropic.com/
   - Gemini: https://aistudio.google.com/apikey
   - GPT: https://platform.openai.com/api-keys
3. (ถ้าต้องการ) เปิด **สแกนอัตโนมัติแบบตั้งเวลา** + ตั้งจำนวนนาที
4. กด **บันทึกการตั้งค่า**

### การใช้งานประจำวัน
- ลากไฟล์รูป/PDF เข้าหน้าเว็บ (หรือก๊อปปี้เข้า `data/inbox/`)
- กด **"สแกนไฟล์ใน Folder ทันที"** — หรือรอให้ระบบตั้งเวลาทำเอง
- ตรวจผลได้ที่ตาราง "ผลการสแกนล่าสุด" และในไฟล์ Excel

## โครงสร้างไฟล์
```
scanner_app/
├── main.py          — FastAPI (หน้าเว็บ + API)
├── config.py        — โหลด/บันทึกการตั้งค่า (config.json)
├── extractor.py     — เรียก AI Vision อ่านฟอร์ม (Claude/Gemini/GPT)
├── excel_writer.py  — เขียนต่อท้าย Excel ตาม schema เดิม
├── scanner.py       — ตรรกะสแกน + ย้ายไฟล์
├── scheduler.py     — ตัวตั้งเวลา (APScheduler)
├── static/index.html— หน้าเว็บ
└── data/
    ├── inbox/    — วางไฟล์รอสแกนที่นี่
    ├── scanned/  — ไฟล์ที่สแกนสำเร็จ
    └── failed/   — ไฟล์ที่สแกนไม่สำเร็จ
```

## หมายเหตุ
- `config.json` เก็บ API key ไว้ในเครื่อง (อยู่ใน .gitignore)
- ปรับ prompt การอ่านฟอร์มได้ที่ `extractor.py` (ตัวแปร `EXTRACTION_PROMPT`)
- ปรับ mapping คอลัมน์ Excel ได้ที่ `excel_writer.py`
- ควรตรวจทานค่าที่ AI อ่านจากลายมือทุกครั้งก่อนใช้จริง โดยเฉพาะตัวเลข
