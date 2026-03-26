# Content QC Tool

Tool nội bộ YouNet tự động xác minh TikTok comment bằng OCR + fuzzy matching. Upload file Excel / kết nối Google Sheet → hệ thống tải screenshot, OCR, so khớp text → xuất kết quả 1/0 ngay trong file.

---

## Tech Stack

| Layer | Công nghệ |
|---|---|
| Backend | Python 3.11, FastAPI, SQLAlchemy + SQLite |
| OCR | EasyOCR (Vietnamese + English), CPU-only PyTorch |
| Fuzzy matching | rapidfuzz + unidecode (xử lý mất dấu tiếng Việt) |
| Frontend | Single-page HTML, Vue 3 CDN, YouNet Brand Identity |
| Reverse proxy | Nginx |
| Deploy | Docker Compose (2 services) |

---

## Yêu cầu

- Docker Desktop ≥ 24 (hoặc Docker Engine + Compose plugin)
- RAM tối thiểu **4 GB** dành cho container backend (EasyOCR + PyTorch CPU)
- Kết nối internet lần đầu để pull image và download EasyOCR models (~800 MB)

---

## Cài đặt & chạy

### 1. Clone repo

```bash
git clone <repo-url>
cd content-qc-tool
```

### 2. Tạo file `.env` (optional)

Mặc định đã có giá trị fallback trong `docker-compose.yml`. Nếu muốn override:

```bash
cp .env.example .env
# Chỉnh sửa .env theo nhu cầu
```

| Biến | Mặc định | Mô tả |
|---|---|---|
| `SECRET_KEY` | `younet-content-qc-secret-key-...` | JWT signing key — **đổi trên production** |
| `DEFAULT_ADMIN_EMAIL` | `it@younetgroup.com` | Email tài khoản admin mặc định |
| `DEFAULT_ADMIN_PASSWORD` | `YouNet@2026` | Mật khẩu admin mặc định — **đổi sau lần đầu** |
| `MAX_WORKERS` | `2` | Số background workers |
| `MATCH_THRESHOLD` | `80` | Ngưỡng fuzzy match (%) để tính là OK |

### 3. Build và start

```bash
docker compose up -d --build
```

> Lần đầu build mất khoảng **5–15 phút** (download PyTorch CPU ~200 MB + EasyOCR models ~600 MB).
> Các lần sau dùng cache, build < 30 giây.

### 4. Truy cập

| URL | Mô tả |
|---|---|
| http://localhost:3000 | Frontend (Vue app) |
| http://localhost:8000/docs | Backend API (Swagger UI) |

Đăng nhập với tài khoản admin mặc định, sau đó đổi mật khẩu tại tab **Admin**.

---

## Sử dụng

### Upload Excel

1. Chọn file `.xlsx` có cột **Comment** và cột **Link Screenshot**
2. Chọn sheet (nếu file có nhiều sheet)
3. Chọn đúng cột Comment và cột Screenshot URL
4. Bấm **Start Processing**

> **Quy tắc gộp row:** Nếu nhiều comment dùng chung 1 screenshot, chỉ cần điền URL ở row đầu — các row bên dưới không có URL sẽ tự kế thừa URL gần nhất phía trên.

### Google Sheet

Xem phần [Kết nối Google Sheet](#kết-nối-google-sheet) bên dưới.

### Kết quả

- **OK (1):** Comment tìm thấy trong screenshot với độ khớp ≥ threshold
- **No (0):** Không tìm thấy hoặc độ khớp thấp hơn threshold
- **Skipped:** Row không có screenshot URL sau khi kế thừa (không có URL nào phía trên)
- **Failed:** Download hoặc OCR lỗi (có thể Resume để retry)

Download file kết quả (.xlsx) từ nút **Download Result** — giữ nguyên file gốc, thêm 2 cột `QC Result` và `OCR Text`.

---

## Kết nối Google Sheet

Tool hỗ trợ đọc dữ liệu trực tiếp từ Google Sheets qua **Service Account** (không cần OAuth).

### Bước 1 — Tạo GCP Project & Service Account

1. Vào [console.cloud.google.com](https://console.cloud.google.com)
2. Tạo project mới (hoặc dùng project có sẵn)
3. Bật **Google Sheets API**:
   - Menu trái → **APIs & Services** → **Library**
   - Tìm "Google Sheets API" → **Enable**
4. Tạo Service Account:
   - Menu trái → **APIs & Services** → **Credentials**
   - **+ CREATE CREDENTIALS** → **Service account**
   - Điền tên (vd: `content-qc-reader`), bấm **Create and continue**
   - **Role:** chọn `Editor` (cần đọc + ghi kết quả về sheet) → **Continue** → **Done**
5. Tạo JSON key:
   - Click vào service account vừa tạo → tab **Keys**
   - **Add Key** → **Create new key** → chọn **JSON** → **Create**
   - File `.json` sẽ được tải về máy — giữ file này bảo mật

> Service account email có dạng: `content-qc-reader@your-project.iam.gserviceaccount.com`

### Bước 2 — Upload credentials vào tool

1. Đăng nhập admin → tab **Admin**
2. Mục **Google Sheets Credentials** → upload file `.json` vừa tải
3. Tool sẽ hiện email của service account — copy lại để dùng ở bước tiếp theo

### Bước 3 — Share Google Sheet với service account

1. Mở Google Sheet cần QC
2. Bấm **Share** (góc trên phải)
3. Paste email service account vào ô (vd: `content-qc-reader@...`)
4. Chọn quyền **Editor** (cần để ghi kết quả QC Result + OCR Text về sheet) → **Send**

> Không cần share lại nếu cùng service account — mọi sheet đều share 1 lần là xong.

### Bước 4 — Tạo job từ Google Sheet

1. Tab **New Job** → chọn chế độ **Google Sheet**
2. Paste URL Google Sheet (bất kỳ format nào):
   - `https://docs.google.com/spreadsheets/d/SHEET_ID/edit`
   - `https://docs.google.com/spreadsheets/d/SHEET_ID/edit#gid=0`
3. Chọn sheet, cột Comment, cột Screenshot
4. **Start Processing**
5. Sau khi xong: kết quả được **ghi lại thẳng vào Google Sheet** (cột QC Result + OCR Text)

---

## Định dạng screenshot hỗ trợ

| Nguồn | Ví dụ URL | Xử lý |
|---|---|---|
| Google Drive | `https://drive.google.com/file/d/FILE_ID/view` | Auto convert → direct download |
| Gyazo | `https://gyazo.com/HASH` | Auto convert → `i.gyazo.com/HASH.png` |
| Imgur | `https://imgur.com/ID` | Auto convert → `i.imgur.com/ID.png` |
| Dropbox | `https://www.dropbox.com/s/...?dl=0` | Auto convert → `dl=1` |
| Direct URL | `https://example.com/image.png` | Dùng trực tiếp |

**Smart crop cho TikTok desktop:** Ảnh chụp màn hình TikTok web (landscape, tỉ lệ > 1.4) sẽ tự động crop lấy phần bên phải (comments panel) trước khi OCR — bỏ qua video và sidebar để tăng độ chính xác.

---

## Cấu trúc thư mục

```
content-qc-tool/
├── docker-compose.yml
├── .env.example
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py          # FastAPI routes
│       ├── processor.py     # Excel parse + job orchestration
│       ├── ocr_service.py   # Image download + OCR subprocess
│       ├── ocr_worker.py    # Persistent EasyOCR worker (subprocess)
│       ├── matcher.py       # Fuzzy matching logic
│       ├── gsheet_service.py# Google Sheets integration
│       ├── database.py      # SQLAlchemy models + migrations
│       ├── auth.py          # JWT auth
│       ├── schemas.py       # Pydantic schemas
│       └── config.py        # Settings
├── frontend/
│   ├── Dockerfile
│   ├── nginx.conf
│   └── public/
│       └── index.html       # Vue 3 SPA (single file)
└── data/                    # Runtime data (gitignore)
    ├── uploads/             # File Excel đã upload
    ├── results/             # File kết quả
    ├── image_cache/         # Screenshot đã tải (cache)
    └── content_qc.db        # SQLite database
```

---

## Vận hành

### Logs

```bash
docker logs content-qc-backend -f
docker logs content-qc-frontend -f
```

### Restart

```bash
docker compose restart
```

### Rebuild sau khi thay đổi code

```bash
docker compose build && docker compose up -d
```

### Backup database

```bash
cp data/content_qc.db data/content_qc.db.bak
```

### Xóa image cache (giải phóng disk)

Vào tab **Admin** → **Clear Image Cache**, hoặc:

```bash
docker exec content-qc-backend rm -rf ./data/image_cache/*
```

---

## Troubleshooting

| Vấn đề | Nguyên nhân | Giải pháp |
|---|---|---|
| Build lâu / treo ở torch | Mạng chậm tải PyTorch CPU | Chờ, đây là lần đầu (~200 MB) |
| Job stuck "processing" sau restart | Container bị kill giữa chừng | Tool tự reset về `failed` khi khởi động lại, bấm **Resume** |
| Screenshot lỗi "not a valid image" | URL trả về HTML (Gyazo page, Drive warning) | Đã xử lý tự động; nếu vẫn lỗi — kiểm tra URL trong Excel |
| Google Sheet lỗi "403" | Sheet chưa share với service account | Share sheet với email service account quyền Viewer |
| OCR kết quả kém, mất dấu | Ảnh quá nhỏ hoặc chất lượng thấp | Dùng ảnh ≥ 720px, tránh compress JPEG nhiều lần |
| Match score thấp dù text đúng | Threshold quá cao | Giảm threshold tại **Admin Settings** (mặc định 80%) |

---

## Notes

- Data lưu trong `./data/` — không bị mất khi rebuild Docker image
- OCR chạy trong subprocess riêng (cô lập memory với FastAPI) — tránh OOM kill uvicorn
- Job hỗ trợ **pause / resume** — xử lý tiếp từ chỗ còn dang dở
- Screenshot được cache trên disk — cùng URL không tải lại khi chạy nhiều job
- Xóa job sẽ xóa luôn file upload, file kết quả và image cache liên quan
