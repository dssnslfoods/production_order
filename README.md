# Production Order Scanner

An AI-powered document scanning system for manufacturing production orders. Captures handwritten factory forms via camera or file upload, extracts structured data using vision AI, and exports to Excel — replacing manual data entry entirely.

Built for [NSL Foods Public Company Limited](https://www.nslfoods.com).

## Overview

Production Order Scanner digitizes handwritten material requisition forms (ใบเบิกวัตถุดิบ) used on the factory floor. Workers photograph paper forms; the system reads order numbers, item descriptions, quantities, and warehouse codes using AI vision — then stores the structured data for review, approval, and export.

### Key Features

- **AI Vision OCR** — Reads handwritten Thai/English factory forms using Claude, Gemini, or GPT (configurable)
- **Auto-crop** — Detects dashed separator lines on forms and crops to the essential data columns before AI processing, reducing token cost and improving accuracy
- **Auto-orientation** — Detects and corrects rotated or upside-down scans via 2-vote AI consensus
- **3-State Approval Workflow** — `pending_approval` → `draft` → `approved` with role-based permissions
- **Role-Based Access Control** — Admin, Supervisor, and Staff roles with a configurable permission matrix
- **Excel Export** — Generates `.xlsx` files with auto-filters, frozen headers, and multi-sheet output matching the factory's existing template
- **Duplicate Detection** — Prevents duplicate entries by matching on Production Order number
- **Queue System** — Upload-now, scan-later architecture with automatic retry and dead letter queue for persistent failures
- **Google Drive Integration** — Optionally pulls new files from a shared Drive folder on each scan cycle
- **Scheduled Scanning** — Cloud Scheduler triggers automatic processing at configurable intervals
- **Activity Audit Log** — Tracks all user actions with 90-day retention policy

## Architecture

```
Browser/Mobile ──► Firebase Hosting (SPA)
                        │
                        ▼
                   Cloud Run (FastAPI)
                   ├── Firestore       (orders, users, settings, activity logs)
                   ├── Cloud Storage   (scanned images)
                   ├── Firebase Auth   (email/password)
                   ├── Cloud Scheduler (cron auto-scan)
                   ├── AI Vision API   (Claude / Gemini / GPT)
                   └── Google Drive    (optional source folder)

Local Scanner ──► ~/nsl_scanner (launchd service, writes Excel directly)
```

| Component | Service | Region |
|---|---|---|
| Frontend | Firebase Hosting | Global CDN |
| Backend API | Cloud Run | asia-southeast1 |
| Database | Cloud Firestore | asia-southeast1 |
| File Storage | Cloud Storage | asia-southeast1 |
| Authentication | Firebase Auth | — |
| Scheduler | Cloud Scheduler | asia-southeast1 |

## Project Structure

```
production_order/
├── firebase_scanner/          # Cloud deployment (primary)
│   ├── public/                # Frontend SPA
│   │   └── index.html         # Single-page application
│   ├── server/                # Backend API
│   │   ├── main.py            # FastAPI endpoints
│   │   ├── extractor.py       # AI vision extraction + auto-crop
│   │   ├── firestore_store.py # Data layer (Firestore + Storage)
│   │   ├── excel_export.py    # Excel workbook builder
│   │   ├── auth.py            # Firebase Auth token verification
│   │   ├── drive_puller.py    # Google Drive file ingestion
│   │   ├── scheduler_admin.py # Cloud Scheduler management
│   │   ├── Dockerfile         # Container definition
│   │   ├── requirements.txt   # Python dependencies
│   │   └── tests/             # Unit tests (143 tests)
│   ├── firebase.json          # Firebase configuration
│   ├── firestore.rules        # Security rules
│   ├── storage.rules          # Storage security rules
│   └── deploy.sh              # One-command deployment script
│
└── scanner_app/               # Local deployment (standalone)
    ├── main.py                # Local web server
    ├── scanner.py             # File watcher + processor
    ├── extractor.py           # AI vision extraction
    ├── excel_writer.py        # Direct Excel file writer
    └── com.nsl.scanner.plist  # macOS launchd service definition
```

## Prerequisites

- Google Cloud account with billing enabled (Blaze plan)
- [Firebase CLI](https://firebase.google.com/docs/cli): `npm i -g firebase-tools`
- [Google Cloud SDK](https://cloud.google.com/sdk/docs/install)
- Python 3.9+
- At least one AI provider API key (Anthropic, Google AI, or OpenAI)

## Deployment

```bash
cd firebase_scanner
cp config.env.example config.env   # Edit with your project values
gcloud auth login
firebase login
./deploy.sh
```

The deploy script enables required APIs, builds and deploys the Cloud Run container, and deploys Firebase Hosting with Firestore/Storage security rules.

After deployment, access the application at `https://<PROJECT_ID>.web.app`.

For detailed setup instructions, see [`firebase_scanner/README.md`](firebase_scanner/README.md).

## Development

### Running Tests

```bash
cd firebase_scanner/server
pip install -r requirements.txt
python -m pytest tests/ -v
```

All 143 tests pass, covering API endpoints, data layer logic, extraction pipeline, image optimization, pagination, error handling, and the dead letter queue.

### Updating

- **Backend changes**: `gcloud run deploy production-order-api --source server --region asia-southeast1`
- **Frontend changes**: `firebase deploy --only hosting`
- **Full redeploy**: `./deploy.sh`

### Customization

| What | Where |
|---|---|
| AI extraction prompt | `server/extractor.py` → `EXTRACTION_PROMPT` |
| Excel export schema | `server/excel_export.py` |
| Data model | `server/firestore_store.py` |
| Permission defaults | `server/firestore_store.py` → `DEFAULT_PERMISSIONS` |
| UI / Frontend | `public/index.html` |

## Cost Estimate

For low-volume usage (< 100 scans/day):

| Service | Estimated Cost |
|---|---|
| Cloud Run | Scale-to-zero; typically < $1/month |
| Firestore + Storage | Within free tier for most usage |
| AI Vision API | Per-scan cost varies by provider |

Cloud Run only incurs charges during active requests, making it extremely cost-effective for intermittent scanning workloads.

## License

Copyright (c) 2024-2026 Arnon Arpaket. All rights reserved.

This software is proprietary and confidential. Unauthorized use, copying, modification, distribution, or reproduction of this software, in whole or in part, by any means, is strictly prohibited without the prior written consent of the copyright holder.

For licensing inquiries, contact: arpaket@gmail.com
