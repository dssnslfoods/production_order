# ระบบสแกนใบเบิกวัตถุดิบ — เวอร์ชัน Firebase (Cloud)

ถ่ายรูปฟอร์มจากมือถือ → AI อ่านข้อมูล → เก็บใน Firestore → กด Export เป็น Excel ได้ทุกเมื่อ
เข้าใช้งานจากที่ไหนก็ได้ผ่านเว็บ (ไม่ต้องพึ่ง Mac เครื่องใดเครื่องหนึ่ง)

## สถาปัตยกรรม
```
📱 มือถือ ──► 🌐 Firebase Hosting (หน้าเว็บ + Login)
                     │  (เรียก /api/** ผ่าน rewrite)
                     ▼
              ⚙️ Cloud Run (FastAPI + AI Vision)
                 ├─► 🗄️ Cloud Storage  (เก็บรูปต้นฉบับ)
                 └─► 🔥 Firestore       (เก็บข้อมูลที่สแกน)
                          │
                          ▼  ปุ่ม Export
                     📊 ไฟล์ .xlsx (schema เดิม 3 sheet)
```
- **Auth**: Firebase Auth (email/password) — ทุก API ต้องมี token ที่ login แล้ว
- **ความปลอดภัย**: Firestore/Storage ปิด client ตรงทั้งหมด เข้าถึงผ่าน backend เท่านั้น
- **API keys**: เก็บเป็น env var ของ Cloud Run (ไม่อยู่ในโค้ด/เว็บ)

---

## สิ่งที่ต้องเตรียม (ครั้งเดียว)
1. บัญชี Google + บัตรเครดิต (เปิด Blaze plan)
2. **Node.js** (สำหรับ firebase CLI): `npm i -g firebase-tools`
3. **gcloud CLI**: https://cloud.google.com/sdk/docs/install

---

## ขั้นตอน Deploy (ทำทีละขั้น)

### 1) สร้าง Firebase project
- ไปที่ https://console.firebase.google.com → **Add project**
- เปิด **Blaze plan** (Settings → Usage and billing) — จำเป็นสำหรับ Cloud Run
- จดค่า **Project ID** ไว้

### 2) เปิดบริการในโปรเจกต์
- **Build → Firestore Database** → Create database → โหมด **Production** → region `asia-southeast1`
- **Build → Storage** → Get started
- **Build → Authentication** → Get started → เปิด **Email/Password**
- **Authentication → Users → Add user** → สร้างบัญชีสำหรับพนักงาน (อีเมล+รหัสผ่าน)

### 3) เอาค่า Firebase config ใส่หน้าเว็บ
- **Project settings (⚙️) → General → Your apps → Web app (</>)** → สร้าง/เปิดดู
- คัดลอก object `firebaseConfig` มาวางแทน `FIREBASE_CONFIG` ใน **`public/index.html`**

### 4) กรอกค่า deploy
```bash
cd firebase_scanner
cp config.env.example config.env      # แล้วแก้ค่าในไฟล์
#  - PROJECT_ID       = project id จากขั้น 1
#  - ALLOWED_EMAILS   = อีเมลที่อนุญาต (คั่น comma)
#  - CLAUDE/GEMINI/OPENAI_API_KEY = ใส่เฉพาะที่จะใช้
```
แก้ `.firebaserc` → ใส่ Project ID แทน `PASTE_YOUR_PROJECT_ID`

### 5) Login แล้ว deploy
```bash
gcloud auth login
firebase login
./deploy.sh
```
สคริปต์จะ: เปิด API → build+deploy Cloud Run → deploy Hosting + rules
เสร็จแล้วเปิด **https://<PROJECT_ID>.web.app** บนมือถือได้เลย

---

## การใช้งาน
1. เปิดเว็บ → login ด้วยบัญชีที่สร้างไว้
2. กด **ถ่ายรูป/เลือกไฟล์** → ถ่ายฟอร์ม → ระบบอ่านและบันทึกอัตโนมัติ
3. ดูรายการที่สแกนแล้ว + กด **Export เป็น Excel** เมื่อต้องการ
4. เปลี่ยน provider AI ได้ในหน้า "ตั้งค่า AI"

---

## ค่าใช้จ่าย (โดยประมาณ สำหรับปริมาณน้อย)
| บริการ | ค่าใช้จ่าย |
|---|---|
| Cloud Run | scale-to-zero, มี free tier — มักไม่กี่บาท/เดือน |
| Firestore + Storage | free tier ครอบคลุมการใช้งานทั่วไป |
| AI Vision API | จ่ายตามจำนวนรูปที่สแกน (แยกตาม provider) |

> Cloud Run รันเฉพาะตอนมีการเรียก จึงถูกมากถ้าสแกนวันละไม่กี่ครั้ง

---

## อัปเดตโค้ดภายหลัง
- แก้ backend → `./deploy.sh` (deploy ใหม่ทั้งหมด) หรือเฉพาะ Cloud Run:
  `gcloud run deploy scanner-api --source server --region asia-southeast1`
- แก้หน้าเว็บ → `firebase deploy --only hosting`

## ปรับแต่ง
- prompt อ่านฟอร์ม: `server/extractor.py` (`EXTRACTION_PROMPT`)
- schema Excel ที่ export: `server/excel_export.py`
- โครงสร้างข้อมูล Firestore: `server/firestore_store.py`

## แก้ปัญหาเบื้องต้น
- **เว็บขึ้นแถบเตือน config**: ยังไม่ได้กรอก `FIREBASE_CONFIG` ใน `public/index.html`
- **สแกนแล้วขึ้น "ยังไม่ได้ตั้ง API key"**: ใส่คีย์ใน `config.env` แล้ว `./deploy.sh` ใหม่
- **login ไม่ได้**: ยังไม่ได้เปิด Email/Password หรือยังไม่ได้สร้าง user ใน Auth
- **403 ไม่มีสิทธิ์**: อีเมลไม่อยู่ใน `ALLOWED_EMAILS`
- **ดู log Cloud Run**: `gcloud run services logs read scanner-api --region asia-southeast1`
