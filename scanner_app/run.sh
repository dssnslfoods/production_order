#!/bin/bash
# เริ่มระบบสแกนใบเบิกวัตถุดิบ แล้วเปิดเบราว์เซอร์ที่ http://127.0.0.1:8000
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi
exec .venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000
