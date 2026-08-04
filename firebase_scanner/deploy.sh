#!/bin/bash
# Deploy ระบบสแกนขึ้น Firebase + Cloud Run
# ต้องมี: gcloud CLI, firebase CLI, และ login แล้ว (gcloud auth login / firebase login)
set -e
cd "$(dirname "$0")"

[ -f config.env ] || { echo "✗ ยังไม่มี config.env — คัดลอกจาก config.env.example แล้วกรอกค่าก่อน"; exit 1; }
set -a; source config.env; set +a
: "${PROJECT_ID:?ต้องกำหนด PROJECT_ID ใน config.env}"
REGION="${REGION:-asia-southeast1}"
STORAGE_BUCKET="${STORAGE_BUCKET:-$PROJECT_ID.appspot.com}"

echo "== [1/4] ตั้ง project = $PROJECT_ID =="
gcloud config set project "$PROJECT_ID" >/dev/null

echo "== [2/4] เปิด API ที่จำเป็น =="
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com \
  firestore.googleapis.com storage.googleapis.com

echo "== [3/4] deploy backend → Cloud Run (scanner-api) @ $REGION =="
gcloud run deploy scanner-api \
  --source server \
  --region "$REGION" \
  --allow-unauthenticated \
  --memory 2Gi \
  --cpu 1 \
  --timeout 3600 \
  --set-env-vars "STORAGE_BUCKET=$STORAGE_BUCKET,ALLOWED_EMAILS=$ALLOWED_EMAILS,CLAUDE_API_KEY=$CLAUDE_API_KEY,GEMINI_API_KEY=$GEMINI_API_KEY,OPENAI_API_KEY=$OPENAI_API_KEY"

echo "== [4/4] deploy frontend + rules → Firebase Hosting =="
firebase deploy --only hosting,firestore:rules,storage --project "$PROJECT_ID"

echo ""
echo "✓ เสร็จ! เปิดเว็บที่:  https://$PROJECT_ID.web.app"
echo "  (อย่าลืมกรอก FIREBASE_CONFIG ใน public/index.html และสร้าง user ใน Firebase Auth)"
