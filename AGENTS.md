# Content QC Tool — Project Intelligence

## Bản chất project
Tool nội bộ để **tự động xác minh TikTok comment** bằng OCR + fuzzy matching. Thay vì người dùng mở từng screenshot ra mắt đọc, tool sẽ OCR hình → so khớp mờ với comment kỳ vọng → xuất kết quả 1/0.

## Tech Stack (bắt buộc)
- **Backend:** Python 3.11+ / FastAPI / SQLAlchemy + SQLite
- **OCR Engine:** EasyOCR (Vietnamese + English) — chạy trong Docker, model pre-download lúc build
- **Fuzzy Matching:** rapidfuzz + unidecode (xử lý mất dấu tiếng Việt)
- **Frontend:** Single-page HTML, Vue 3 CDN, custom CSS theo YouNet Brand Identity
- **Reverse Proxy:** Nginx
- **Deploy:** Docker Compose (2 services: backend, frontend/nginx)

## Conventions
- Backend code trong `backend/app/` — mỗi module 1 responsibility
- Frontend là 1 file `index.html` duy nhất trong `frontend/public/`
- API prefix: `/api/`
- Nginx proxy `/api/*` → `backend:8000`
- Data persist: `./data/uploads/`, `./data/results/`, `./data/qc.db`
- Default admin: `it@younetgroup.com` / `YouNet@2026`

## Lưu ý quan trọng khi code
1. **Một screenshot chứa nhiều comment** — group rows theo screenshot URL, OCR 1 lần rồi match tất cả comment thuộc group đó
2. **Fuzzy matching phải xử lý:** sai dấu tiếng Việt, thiếu space, punctuation khác, viết không dấu
3. **Retry + Resume:** exponential backoff khi download fail, job pause/resume chạy tiếp từ row cuối
4. **Google Drive links** cần convert sang direct download URL
5. Brand Identity file tham chiếu: `../younet-brand-identity.md`
