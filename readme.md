# GRN → KRA eTIMS Backend

Production-grade FastAPI backend for GRN processing and eTIMS automation.

---

## Stack

| Layer | Technology |
|---|---|
| Framework | FastAPI + Uvicorn |
| Database | PostgreSQL (async via asyncpg) |
| ORM | SQLAlchemy 2.0 (async) |
| Migrations | Alembic |
| Background Jobs | Celery + Redis |
| Auth | JWT (python-jose + passlib/bcrypt) |
| File Storage | Local filesystem (S3-ready) |
| GRN Extraction | pdfplumber (PDF) + PaddleOCR (images) |
| eTIMS Automation | Selenium + ChromeDriver |

---

## Setup

### 1. Clone and install

```bash
cd backend
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — set DATABASE_URL, ETIMS_USER_ID, ETIMS_PASSWORD, APP_SECRET_KEY
```

### 3. Create the database

```bash
createdb grn_db
```

### 4. Run migrations

```bash
alembic upgrade head
```

### 5. Start the API server

```bash
uvicorn app.main:app --reload --port 8000
```

API docs: http://localhost:8000/docs

### 6. Start the Celery worker (separate terminal)

```bash
celery -A app.workers.etims_tasks worker \
    --loglevel=info \
    --queues=etims \
    --concurrency=1
```

> `--concurrency=1` because each task opens a Chrome browser.

### 7. Start Redis (if not running)

```bash
# macOS
brew services start redis

# Ubuntu
sudo systemctl start redis
```

---

## API Endpoints

### Auth
| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/auth/login` | Get access + refresh tokens |
| POST | `/api/v1/auth/refresh` | Refresh access token |
| GET | `/api/v1/auth/me` | Current user profile |

### GRNs
| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/grns/upload` | Upload PDF or image → auto-extract |
| GET | `/api/v1/grns` | List GRNs (filter by `?status=`) |
| GET | `/api/v1/grns/{id}` | Get single GRN |
| POST | `/api/v1/grns/{id}/confirm` | Confirm extracted data + queue eTIMS |
| POST | `/api/v1/grns/{id}/reject` | Reject a GRN |

### eTIMS Invoices
| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/etims-invoices` | List invoices (filter by `?status=`) |
| GET | `/api/v1/etims-invoices/{id}` | Get single invoice |
| POST | `/api/v1/etims-invoices/{id}/retry` | Retry rejected submission |

---

## GRN State Machine

```
uploaded → extracted → pending_confirmation → confirmed → invoiced
                                           ↘ rejected
```

## eTIMS State Machine

```
pending → submitted → approved
                   ↘ rejected (retryable up to 3x)
```

---

## File Layout

```
backend/
├── app/
│   ├── main.py               ← FastAPI app factory
│   ├── config.py             ← All env vars (pydantic-settings)
│   ├── db/
│   │   ├── session.py        ← Async DB session + get_db dependency
│   │   ├── base.py           ← DeclarativeBase + mixins
│   │   └── models/
│   │       ├── user.py       ← User + roles
│   │       ├── grn.py        ← GRN + state machine
│   │       └── etims_invoice.py
│   ├── schemas/
│   │   ├── grn.py            ← Pydantic request/response models
│   │   └── etims.py
│   ├── api/
│   │   ├── deps.py           ← Auth dependencies, pagination
│   │   ├── auth.py           ← Login, refresh, me
│   │   ├── grns.py           ← GRN endpoints
│   │   └── etims.py          ← eTIMS invoice endpoints
│   ├── services/
│   │   ├── file_storage.py   ← Save/retrieve uploaded files
│   │   ├── grn_extractor.py  ← Wraps read_pdf.py + read_image_content.py
│   │   └── etims_mapper.py   ← Converts GRN → KRA payload
│   ├── core/
│   │   ├── security.py       ← JWT + password hashing
│   │   ├── logging.py        ← Structured logging setup
│   │   └── exceptions.py     ← Custom HTTP exceptions
│   └── workers/
│       └── etims_tasks.py    ← Celery task: submit_to_etims
├── migrations/
│   └── env.py                ← Alembic async config
├── storage/
│   ├── grns/                 ← Uploaded GRN files
│   └── invoices/             ← Generated invoice PDFs
├── requirements.txt
└── .env.example
```

---

## Adding Your Existing Scripts

Place these files in the `backend/` root (same level as `app/`):

- `read_pdf.py`
- `read_image_content.py`
- `read_salesReceipt.py`
- `fill_kra.py`

The services layer imports them directly. No modification needed.

---

## Next Steps

1. Upload `database.sql` → generate remaining SQLAlchemy models
2. Add Order model + CRUD endpoints
3. Add sync endpoint (`GET /sync?last_sync=timestamp`) for Flutter
4. Wire eTIMS REST API (when available) to replace Selenium

&& python -m app.db.seed_users