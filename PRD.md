# PRD: Content QC Tool — TikTok Comment Verification

## 1. Problem Statement

### Hiện trạng (AS-IS)
Team Content QC đang làm **hoàn toàn manual**: mở từng hình screenshot trong cột O của Google Sheet, đọc bằng mắt xem comment trong cột K có xuất hiện trong ảnh TikTok comment hay không. Một số người tốt bụng thì khoanh đỏ hoặc ghi note ai comment, nhưng đa phần là mắt đọc tay ghi.

### Đặc thù dữ liệu
- File Excel export từ Google Sheets
- **Cột K** = comment text kỳ vọng (tiếng Việt, có dấu hoặc không dấu)
- **Cột O** = link screenshot (thường là Google Drive link hoặc direct image URL)
- **QUAN TRỌNG:** Nhiều dòng liên tiếp có cùng URL ở cột O — vì 1 screenshot chứa nhiều comment TikTok, mỗi dòng Excel là 1 comment cần verify trong cùng 1 ảnh. Tool phải group lại, OCR 1 lần, match nhiều comment.

### Mục tiêu (TO-BE)
Web app tự động: upload Excel → OCR screenshot → fuzzy match comment → xuất file kết quả 1/0 cho mỗi dòng.

---

## 2. Tech Stack

| Layer | Technology | Lý do chọn |
|-------|-----------|-------------|
| Backend | **Python 3.11 + FastAPI** | Async native, tốt cho I/O-bound (download ảnh), ecosystem ML/OCR mạnh |
| OCR | **EasyOCR** (vi + en) | Open-source, hỗ trợ tiếng Việt tốt nhất trong các lựa chọn self-hosted, chạy CPU được, model ~60MB |
| Fuzzy Match | **rapidfuzz** + **unidecode** | Nhanh hơn fuzzywuzzy 10x, unidecode xử lý bỏ dấu tiếng Việt |
| Database | **SQLite** via SQLAlchemy | Đơn giản, không cần DB server riêng, đủ cho tool nội bộ |
| Auth | **JWT** (python-jose + passlib bcrypt) | Stateless, đủ cho admin auth |
| Excel | **openpyxl** | Đọc/ghi .xlsx với formatting |
| Frontend | **Vue 3 CDN** + custom CSS | Reactive UI, không cần build step, 1 file HTML |
| Reverse Proxy | **Nginx** | Serve static + proxy API, handle upload size |
| Deploy | **Docker Compose** | 2 services, 1 command chạy |

---

## 3. Architecture

```
┌─────────────────────────────────────────────┐
│                  Browser                     │
│   Vue 3 SPA (index.html)                    │
└───────────────┬─────────────────────────────┘
                │ HTTP
┌───────────────▼─────────────────────────────┐
│           Nginx (:3000)                      │
│   - Serve /  → static HTML                  │
│   - Proxy /api/* → backend:8000             │
└───────────────┬─────────────────────────────┘
                │
┌───────────────▼─────────────────────────────┐
│       FastAPI Backend (:8000)                │
│                                              │
│  ┌──────────┐ ┌──────────┐ ┌─────────────┐ │
│  │ Auth     │ │ Jobs API │ │ Admin API   │ │
│  │ (JWT)    │ │ (CRUD)   │ │ (Settings)  │ │
│  └──────────┘ └────┬─────┘ └─────────────┘ │
│                     │                        │
│         ┌───────────▼──────────────┐        │
│         │    Processor Engine      │        │
│         │  ┌─────────┐ ┌────────┐ │        │
│         │  │EasyOCR  │ │Matcher │ │        │
│         │  │(vi+en)  │ │(fuzzy) │ │        │
│         │  └─────────┘ └────────┘ │        │
│         └──────────────────────────┘        │
│                     │                        │
│         ┌───────────▼──────────────┐        │
│         │   SQLite (qc.db)         │        │
│         │   - users, jobs, rows    │        │
│         │   - admin_settings       │        │
│         └──────────────────────────┘        │
└─────────────────────────────────────────────┘
```

---

## 4. Functional Requirements

### 4.1 File Upload & Preview
- [ ] Accept `.xlsx` / `.xls` upload (max 50MB)
- [ ] Lưu file vào `./data/uploads/` với UUID filename
- [ ] Preview endpoint trả về **3 dòng đầu** để user chọn header line
- [ ] Hiển thị tất cả column letters (A, B, C, ..., Z, AA, ...)

### 4.2 Job Configuration (trước khi chạy)
- [ ] **Header Line:** Chọn dòng 1 hoặc 2 làm header (radio button trên preview table)
- [ ] **Comment Column:** Default `K`, dropdown chọn từ danh sách cột (hiển thị tên header)
- [ ] **Screenshot Column:** Default `O`, dropdown tương tự
- [ ] **Result Column:** User chọn cột ghi kết quả 1/0 (suggest cột tiếp theo sau cột cuối)
- [ ] **OCR Dump Column (optional):** Nếu chọn, ghi toàn bộ text OCR được vào cột này
- [ ] **Parallel Workers:** Dropdown 1-5, default `2`
- [ ] **Match Threshold:** Input number, default lấy từ Admin Settings (80%), cho phép override per-job

### 4.3 Processing Engine

#### 4.3.1 Grouping
- [ ] Parse Excel từ dòng header+1 trở xuống
- [ ] Group tất cả rows có cùng screenshot URL
- [ ] Mỗi group chỉ download + OCR **1 lần**

#### 4.3.2 Image Download
- [ ] Detect và convert Google Drive share links → direct download URL:
  - `https://drive.google.com/file/d/{ID}/view` → `https://drive.google.com/uc?id={ID}&export=download`
  - `https://drive.google.com/open?id={ID}` → tương tự
- [ ] Support direct image URLs (jpg, png, webp, etc.)
- [ ] **Retry:** Exponential backoff — wait 2s, 4s, 8s, max 3 retries per image
- [ ] **Timeout:** 60s per download (configurable in Admin)
- [ ] **Cache:** In-memory LRU cache cho images đã download trong session, tránh re-download

#### 4.3.3 OCR
- [ ] EasyOCR reader với languages `['vi', 'en']`
- [ ] Initialize reader **1 lần** khi app startup (không tạo mới mỗi request)
- [ ] OCR trả về list of text blocks → join thành 1 string (newline separated)
- [ ] Handle multi-line comments: mỗi text block là 1 dòng riêng, giữ nguyên thứ tự
- [ ] Return full OCR text để ghi vào OCR dump column nếu cần

#### 4.3.4 Fuzzy Matching
6 chiến lược matching theo thứ tự ưu tiên (dừng khi match):

1. **Exact substring** (normalized, có dấu): normalize whitespace + lowercase + strip punctuation → check `comment in ocr_text`
2. **Exact substring** (bỏ dấu): unidecode cả 2 → check substring
3. **Fuzzy partial ratio** (có dấu): `rapidfuzz.fuzz.partial_ratio` ≥ threshold
4. **Fuzzy partial ratio** (bỏ dấu): tương tự nhưng unidecode
5. **Token sort ratio** (bỏ dấu): `rapidfuzz.fuzz.token_sort_ratio` ≥ threshold
6. **Per-line matching**: tách OCR text thành từng dòng, fuzzy match từng dòng riêng

- [ ] Trả về: `(is_match: bool, score: float, strategy: str)`
- [ ] Threshold default 80%, configurable

#### 4.3.5 Parallelism
- [ ] Dùng `asyncio.Semaphore` để control concurrent processing
- [ ] Mỗi "task" = 1 screenshot group (download + OCR + match all comments)
- [ ] Default 2 concurrent tasks, user chọn 1-5

### 4.4 Result Output
- [ ] Copy file Excel gốc → ghi thêm cột Result (1/0)
- [ ] Cột Result: `1` = comment found (font xanh bold), `0` = not found (font đỏ bold), `ERROR` nếu fail
- [ ] Nếu có OCR dump column: ghi full OCR text
- [ ] Header row: "QC Result" (bold) và "OCR Text" (bold)
- [ ] Lưu file vào `./data/results/`
- [ ] Cho phép download qua API

### 4.5 Resume & Retry
- [ ] Mỗi row có status: `pending` → `processing` → `completed` / `failed` / `skipped`
- [ ] Mỗi row track `retry_count`
- [ ] **Pause:** User bấm Pause → set flag, processor dừng sau group hiện tại
- [ ] **Resume:** User bấm Resume → reset failed rows (retry_count < 5) về pending → chạy lại
- [ ] Job có thể resume sau khi restart Docker (data persist trong SQLite)
- [ ] Job statuses: `pending` → `processing` → `completed` / `failed` / `paused`

### 4.6 Job Management
- [ ] List all jobs (newest first), with status dot color-coded
- [ ] View job detail: table tất cả rows với comment, URL, score, result, status, error
- [ ] Filter rows by status
- [ ] Delete job (cleanup files)
- [ ] Download result file

### 4.7 Admin Settings
- [ ] **Login required** — JWT auth
- [ ] Default admin: `it@younetgroup.com` / `YouNet@2026` (created on first startup)
- [ ] Configurable settings (stored in DB, editable via UI):
  - `match_threshold`: default `80` — % fuzzy match để consider là 1
  - `max_retries`: default `3` — số lần retry download ảnh
  - `download_timeout`: default `60` — timeout download (giây)
- [ ] Change admin password
- [ ] Clear image cache

### 4.8 Progress & Monitoring
- [ ] Real-time progress bar (poll every 2s)
- [ ] Stats: total rows, matched, unmatched, failed
- [ ] Error messages hiển thị cho failed rows
- [ ] Toast notifications cho events (upload ok, job start, job complete, errors)

---

## 5. Database Schema

```sql
-- Users table (admin only for now)
CREATE TABLE users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT UNIQUE NOT NULL,
    hashed_password TEXT NOT NULL,
    is_admin    BOOLEAN DEFAULT 0,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Admin settings (key-value)
CREATE TABLE admin_settings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT UNIQUE NOT NULL,
    value       TEXT NOT NULL,
    description TEXT
);

-- Jobs
CREATE TABLE jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    filename          TEXT NOT NULL,          -- UUID filename on disk
    original_filename TEXT NOT NULL,          -- User's original filename
    status            TEXT DEFAULT 'pending', -- pending/processing/completed/failed/paused
    total_rows        INTEGER DEFAULT 0,
    processed_rows    INTEGER DEFAULT 0,
    matched_rows      INTEGER DEFAULT 0,
    failed_rows       INTEGER DEFAULT 0,
    header_line       INTEGER DEFAULT 1,
    comment_column    TEXT DEFAULT 'K',
    screenshot_column TEXT DEFAULT 'O',
    result_column     TEXT DEFAULT 'P',
    ocr_dump_column   TEXT,                  -- nullable, optional
    parallel_workers  INTEGER DEFAULT 2,
    match_threshold   REAL DEFAULT 80.0,
    result_filename   TEXT,                  -- result file on disk
    error_message     TEXT,
    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Job rows (1 per Excel data row)
CREATE TABLE job_rows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    row_number      INTEGER NOT NULL,        -- Excel row number
    comment_text    TEXT,
    screenshot_url  TEXT,
    ocr_text        TEXT,                    -- Full OCR output for this screenshot
    match_score     REAL,                    -- Best fuzzy match score (0-100)
    match_result    INTEGER,                 -- 1 = match, 0 = no match, NULL = not processed
    status          TEXT DEFAULT 'pending',  -- pending/processing/completed/failed/skipped
    error_message   TEXT,
    retry_count     INTEGER DEFAULT 0,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_job_rows_job_id ON job_rows(job_id);
CREATE INDEX idx_job_rows_status ON job_rows(status);
```

---

## 6. API Endpoints

```
POST   /api/auth/login                  → { access_token, email, is_admin }
POST   /api/auth/change-password        → [admin] change password

POST   /api/files/upload                → { filename, original_filename, size }
GET    /api/files/preview/{filename}    → { rows[], column_letters[], total_columns }

POST   /api/jobs?filename=X&original_filename=Y  → JobResponse (creates + starts)
GET    /api/jobs                        → JobResponse[]
GET    /api/jobs/{id}                   → JobResponse
GET    /api/jobs/{id}/rows              → JobRowResponse[] (?status=X&skip=0&limit=100)
POST   /api/jobs/{id}/resume            → { message }
POST   /api/jobs/{id}/pause             → { message }
GET    /api/jobs/{id}/download          → File download (.xlsx)
DELETE /api/jobs/{id}                   → { message }

GET    /api/admin/settings              → [admin] AdminSetting[]
PUT    /api/admin/settings              → [admin] update setting
POST   /api/admin/clear-cache           → [admin] clear OCR image cache

GET    /api/health                      → { status: "ok" }
GET    /api/stats                       → { total_jobs, completed_jobs, total_rows_processed }
```

---

## 7. File Structure

```
content-qc-tool/
├── docker-compose.yml
├── .env                          # SECRET_KEY, DEFAULT_ADMIN_EMAIL, DEFAULT_ADMIN_PASSWORD
├── .env.example
├── CLAUDE.md                     # Project context for Claude Code
├── PRD.md                        # This file
│
├── backend/
│   ├── Dockerfile
│   │   - Base: python:3.11-slim
│   │   - Install: libgl1, libglib2.0 (OpenCV deps for EasyOCR)
│   │   - pip install requirements
│   │   - Pre-download EasyOCR models (vi, en) during build
│   │   - CMD: uvicorn app.main:app --host 0.0.0.0 --port 8000
│   │
│   ├── requirements.txt
│   │   fastapi
│   │   uvicorn[standard]
│   │   sqlalchemy
│   │   python-jose[cryptography]
│   │   passlib[bcrypt]
│   │   python-multipart
│   │   openpyxl
│   │   easyocr
│   │   rapidfuzz
│   │   unidecode
│   │   httpx               # async HTTP client for image download
│   │   pillow
│   │
│   └── app/
│       ├── __init__.py
│       ├── main.py          # FastAPI app, startup event, all route registration
│       ├── config.py        # Pydantic Settings (env vars)
│       ├── database.py      # SQLAlchemy engine, session, all models (User, Job, JobRow, AdminSetting)
│       ├── auth.py          # hash_password, verify_password, create_token, get_current_user, require_admin
│       ├── schemas.py       # Pydantic request/response models
│       ├── ocr_service.py   # download_image (async, retry, GDrive convert), ocr_image_full_text, image cache
│       ├── matcher.py       # normalize_text, match_comment_in_ocr (6 strategies)
│       └── processor.py     # parse_excel, group_by_screenshot, process_job (async), write_results, pause/resume
│
├── frontend/
│   ├── Dockerfile           # nginx:alpine, copy HTML + nginx.conf
│   ├── nginx.conf           # Serve static /, proxy /api/ → backend:8000
│   └── public/
│       └── index.html       # Vue 3 SPA, YouNet branded CSS
│
└── data/                    # Docker volume mount
    ├── uploads/
    ├── results/
    └── qc.db
```

---

## 8. Docker Compose

```yaml
services:
  backend:
    build: ./backend
    ports:
      - "8000:8000"        # Optional direct access for debug
    volumes:
      - ./data:/app/data
      - ocr_models:/root/.EasyOCR
    env_file: .env
    restart: unless-stopped

  frontend:
    build: ./frontend
    ports:
      - "3000:80"          # Main entry point
    depends_on:
      - backend
    restart: unless-stopped

volumes:
  ocr_models:              # Persist EasyOCR model downloads
```

**Chạy:** `docker-compose up --build` → truy cập `http://localhost:3000`

---

## 9. Frontend Design

Tuân thủ **YouNet Brand Identity** (`younet-brand-identity.md`):

- **Colors:** Navy `#04115B` cho headings, Blue-700 `#0066B2` cho buttons/links, neutral `#F8F8FB` bg
- **Typography:** Montserrat (headings/labels), Inter (body), Google Fonts CDN
- **Components:** Cards (16px radius, subtle shadow), pill buttons (999px radius), 44px inputs
- **Layout:** Max 1280px, 24px gutters, 8pt spacing system

### Pages/Tabs:
1. **New Job** — Upload → Preview table → Configure columns → Start → Progress bar + stats
2. **Jobs** — List all jobs with status dot (green/blue-pulse/red/gold), actions (pause/resume/download/delete)
3. **Job Detail** — Full row table with score, result, status, error. Filter by status.
4. **Admin** — Settings table (key/value/description), change password, clear cache. **Requires login.**

### UX Flow:
```
Upload .xlsx → Preview 3 rows (chọn header line) → Map columns → Set workers/threshold
    → Start Processing
        → Progress bar polls /api/jobs/{id} every 2s
        → Stats: Total | Matched(1) | Unmatched(0) | Failed
        → Actions: Pause | Resume | Download Result | View Details
```

---

## 10. Key Implementation Notes

### 10.1 Google Drive Link Handling
```python
import re

def convert_gdrive_url(url: str) -> str:
    """Convert Google Drive share/view links to direct download."""
    patterns = [
        r'drive\.google\.com/file/d/([a-zA-Z0-9_-]+)',
        r'drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)',
        r'drive\.google\.com/uc\?.*id=([a-zA-Z0-9_-]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            file_id = match.group(1)
            return f"https://drive.google.com/uc?id={file_id}&export=download"
    return url  # Return as-is if not a GDrive link
```

### 10.2 EasyOCR Initialization (singleton)
```python
import easyocr

_reader = None

def get_reader():
    global _reader
    if _reader is None:
        _reader = easyocr.Reader(['vi', 'en'], gpu=False)
    return _reader
```

### 10.3 Matching Strategy Priority
```
1. exact substring (có dấu)       → score 100
2. exact substring (bỏ dấu)       → score 98
3. fuzzy partial_ratio (có dấu)   → score ≥ threshold
4. fuzzy partial_ratio (bỏ dấu)   → score ≥ threshold
5. token_sort_ratio (bỏ dấu)      → score ≥ threshold
6. per-line partial_ratio          → score ≥ threshold
→ Best score across all strategies returned
```

### 10.4 Screenshot Grouping Logic
```
Excel rows:
  Row 2: comment="hay quá", screenshot="https://drive.google.com/abc"
  Row 3: comment="đúng rồi", screenshot="https://drive.google.com/abc"  ← SAME URL
  Row 4: comment="cảm ơn",   screenshot="https://drive.google.com/abc"  ← SAME URL
  Row 5: comment="tuyệt vời", screenshot="https://drive.google.com/xyz" ← DIFFERENT

→ Group 1: URL abc → download once, OCR once → match 3 comments
→ Group 2: URL xyz → download once, OCR once → match 1 comment
```

### 10.5 Retry with Exponential Backoff
```python
async def download_with_retry(url, max_retries=3, timeout=60):
    for attempt in range(max_retries):
        try:
            return await httpx_client.get(url, timeout=timeout)
        except Exception:
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(2 ** (attempt + 1))  # 2s, 4s, 8s
```

---

## 11. Non-Functional Requirements

- **Performance:** 2 concurrent workers default, xử lý ~100 rows/file ổn
- **Reliability:** Resume sau crash, retry tự động, data persist
- **Security:** JWT cho admin, bcrypt passwords, không expose internal paths
- **Deployment:** 1 command `docker-compose up --build`, chạy trên bất kỳ máy có Docker
- **Extensibility:** Admin settings cho phép thêm config mới mà không cần sửa code
