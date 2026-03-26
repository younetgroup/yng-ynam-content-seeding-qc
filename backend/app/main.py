"""
Content QC Tool — FastAPI Backend
Verifies TikTok comments against screenshots using OCR + fuzzy matching.
"""
import os
import uuid
import asyncio
import logging
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Query, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from .config import settings
from .database import (
    init_db, get_db, Job, JobRow, JobStatus, RowStatus,
    User, AdminSetting,
)
from .auth import (
    hash_password, verify_password, create_access_token,
    get_current_user, require_admin, ensure_default_admin,
)
from .schemas import (
    LoginRequest, TokenResponse, JobCreateRequest, JobResponse,
    JobRowResponse, FilePreviewResponse, PreviewRow,
    AdminSettingUpdate, AdminSettingResponse, ChangePasswordRequest,
    JobRenameRequest,
)
from .processor import (
    read_excel_preview, parse_excel_for_job, process_job,
    pause_job, is_job_active, col_letter_to_index,
)
from .ocr_service import clear_image_cache
from .gsheet_service import (
    is_credentials_configured, get_sheet_info, read_gsheet_rows,
    reset_client, parse_gsheet_url,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Content QC Tool",
    description="YouNet — TikTok Comment Verification via OCR",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Startup ──────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    init_db()
    os.makedirs(settings.upload_dir, exist_ok=True)
    os.makedirs(settings.result_dir, exist_ok=True)

    db = next(get_db())
    ensure_default_admin(db)

    # Seed default admin settings
    defaults = {
        "match_threshold": ("80", "Match percentage threshold (0-100) to consider a comment verified"),
        "max_retries": ("3", "Maximum retry attempts for failed image downloads"),
        "download_timeout": ("60", "Image download timeout in seconds"),
    }
    for key, (val, desc) in defaults.items():
        existing = db.query(AdminSetting).filter(AdminSetting.key == key).first()
        if not existing:
            db.add(AdminSetting(key=key, value=val, description=desc))
    db.commit()

    # Reset stale "processing" jobs left from previous unclean shutdown
    stale_jobs = db.query(Job).filter(Job.status == JobStatus.PROCESSING.value).all()
    for sj in stale_jobs:
        sj.status = JobStatus.FAILED.value
        sj.error_message = "Reset: was stuck in processing after server restart"
        logger.warning(f"Reset stale job #{sj.id} from processing → failed")
    if stale_jobs:
        db.commit()

    db.close()

    logger.info("Content QC Tool started successfully. OCR runs in isolated subprocess.")


# ── Health ───────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "content-qc-tool"}


# ── Auth ─────────────────────────────────────────────────────────────────

@app.post("/api/auth/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email).first()
    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token({"sub": user.email})
    return TokenResponse(
        access_token=token,
        email=user.email,
        is_admin=user.is_admin,
    )


@app.post("/api/auth/change-password")
def change_password(
    req: ChangePasswordRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not verify_password(req.current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    user.hashed_password = hash_password(req.new_password)
    db.commit()
    return {"message": "Password updated"}


# ── File Upload & Preview ────────────────────────────────────────────────

@app.post("/api/files/upload")
async def upload_file(file: UploadFile = File(...)):
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Only .xlsx/.xls files are supported")

    ext = os.path.splitext(file.filename)[1]
    saved_name = f"{uuid.uuid4().hex}{ext}"
    path = os.path.join(settings.upload_dir, saved_name)

    content = await file.read()
    with open(path, "wb") as f:
        f.write(content)

    return {"filename": saved_name, "original_filename": file.filename, "size": len(content)}


@app.get("/api/files/preview/{filename}", response_model=FilePreviewResponse)
def preview_file(filename: str, sheet_name: Optional[str] = None):
    path = os.path.join(settings.upload_dir, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")

    preview = read_excel_preview(path, num_rows=3, sheet_name=sheet_name)
    return FilePreviewResponse(
        filename=filename,
        rows=[PreviewRow(**r) for r in preview["rows"]],
        total_columns=preview["total_columns"],
        column_letters=preview["column_letters"],
        sheet_names=preview.get("sheet_names", []),
    )


# ── Google Sheets ────────────────────────────────────────────────────────

@app.get("/api/gsheet/status")
def gsheet_status():
    configured = is_credentials_configured()
    service_email = None
    if configured:
        try:
            import json
            with open(settings.google_credentials_path) as f:
                creds = json.load(f)
            service_email = creds.get("client_email")
        except Exception:
            pass
    return {"configured": configured, "service_email": service_email}


@app.get("/api/gsheet/preview")
def gsheet_preview(url: str = Query(...), sheet_name: Optional[str] = None):
    if not is_credentials_configured():
        raise HTTPException(status_code=400, detail="Google credentials not configured. Upload via Admin.")
    try:
        info = get_sheet_info(url)
        # If a specific sheet is requested, re-fetch with that sheet
        if sheet_name and sheet_name in info["sheet_names"] and sheet_name != info["active_sheet"]:
            from .gsheet_service import get_gspread_client
            client = get_gspread_client()
            spreadsheet_id, _ = parse_gsheet_url(url)
            spreadsheet = client.open_by_key(spreadsheet_id)
            worksheet = spreadsheet.worksheet(sheet_name)
            all_values = worksheet.get_all_values()
            preview_rows = []
            for i, row in enumerate(all_values[:3]):
                preview_rows.append({"row_number": i + 1, "cells": row})
            max_col = max(len(r) for r in all_values[:3]) if all_values else 1
            from openpyxl.utils import get_column_letter
            col_letters = [get_column_letter(i + 1) for i in range(max_col)]
            info["rows"] = preview_rows
            info["total_columns"] = max_col
            info["column_letters"] = col_letters
            info["active_sheet"] = sheet_name
            info["total_rows"] = len(all_values)
        return info
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Jobs ─────────────────────────────────────────────────────────────────

@app.post("/api/jobs", response_model=JobResponse)
def create_job(
    req: JobCreateRequest,
    filename: str = Query(None),
    original_filename: str = Query(None),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: Session = Depends(get_db),
):
    # Get threshold from admin settings if not provided
    threshold = req.match_threshold
    if threshold is None:
        setting = db.query(AdminSetting).filter(AdminSetting.key == "match_threshold").first()
        threshold = float(setting.value) if setting else 80.0

    is_gsheet = bool(req.gsheet_url)

    if is_gsheet:
        # Google Sheet mode
        if not is_credentials_configured():
            raise HTTPException(status_code=400, detail="Google credentials not configured")
        comment_col_idx = col_letter_to_index(req.comment_column)
        screenshot_col_idx = col_letter_to_index(req.screenshot_column)
        rows_data = read_gsheet_rows(
            req.gsheet_url, req.sheet_name or "Sheet1",
            req.header_line, comment_col_idx, screenshot_col_idx,
            start_row=req.start_row, end_row=req.end_row,
        )
        job_filename = ""
        job_original_filename = f"GSheet: {req.gsheet_url[:60]}"
    else:
        # Excel file mode
        if not filename:
            raise HTTPException(status_code=400, detail="filename is required for Excel mode")
        path = os.path.join(settings.upload_dir, filename)
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail="Uploaded file not found")
        rows_data = parse_excel_for_job(
            path, req.header_line, req.comment_column, req.screenshot_column,
            sheet_name=req.sheet_name, start_row=req.start_row, end_row=req.end_row,
        )
        job_filename = filename
        job_original_filename = original_filename or filename

    job = Job(
        filename=job_filename,
        original_filename=job_original_filename,
        status=JobStatus.PENDING.value,
        total_rows=len(rows_data),
        processed_rows=0,
        matched_rows=0,
        failed_rows=0,
        header_line=req.header_line,
        comment_column=req.comment_column,
        screenshot_column=req.screenshot_column,
        result_column=req.result_column,
        ocr_dump_column=req.ocr_dump_column or None,
        parallel_workers=req.parallel_workers,
        match_threshold=threshold,
        sheet_name=req.sheet_name,
        start_row=req.start_row,
        end_row=req.end_row,
        gsheet_url=req.gsheet_url,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Create row entries
    for rd in rows_data:
        db.add(JobRow(
            job_id=job.id,
            row_number=rd["row_number"],
            comment_text=rd["comment_text"],
            screenshot_url=rd["screenshot_url"],
            status=RowStatus.PENDING.value,
        ))
    db.commit()

    # Start processing in background
    background_tasks.add_task(_run_job, job.id)

    db.refresh(job)
    return job


async def _run_job(job_id: int):
    """Wrapper to run the async processor."""
    await process_job(job_id)


@app.get("/api/jobs")
def list_jobs(
    skip: int = 0,
    limit: int = 10,
    db: Session = Depends(get_db),
):
    total = db.query(Job).count()
    jobs = (
        db.query(Job)
        .order_by(Job.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return {"jobs": jobs, "total": total, "skip": skip, "limit": limit}


@app.get("/api/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.patch("/api/jobs/{job_id}/rename")
def rename_job(job_id: int, req: JobRenameRequest, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.job_name = req.job_name
    db.commit()
    return {"message": "Job renamed", "job_name": req.job_name}


@app.get("/api/jobs/{job_id}/download-original")
def download_original(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.filename:
        raise HTTPException(status_code=404, detail="No uploaded file (Google Sheet job)")
    path = os.path.join(settings.upload_dir, job.filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Original file not found on disk")
    return FileResponse(
        path,
        filename=job.original_filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/jobs/{job_id}/rows")
def get_job_rows(
    job_id: int,
    status: Optional[str] = None,
    result: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    q = db.query(JobRow).filter(JobRow.job_id == job_id)
    if status:
        q = q.filter(JobRow.status == status)
    if result == "ok":
        q = q.filter(JobRow.match_result == 1)
    elif result == "no":
        q = q.filter(JobRow.match_result == 0, JobRow.status == RowStatus.COMPLETED.value)
    total = q.count()
    rows = q.order_by(JobRow.row_number).offset(skip).limit(limit).all()
    return {"rows": rows, "total": total, "skip": skip, "limit": limit}


@app.post("/api/jobs/{job_id}/resume")
def resume_job(
    job_id: int,
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in [JobStatus.PAUSED.value, JobStatus.FAILED.value, JobStatus.COMPLETED.value]:
        raise HTTPException(status_code=400, detail="Job cannot be resumed from current state")
    if is_job_active(job_id):
        raise HTTPException(status_code=400, detail="Job is already running")

    # Reset failed rows for retry
    failed_rows = (
        db.query(JobRow)
        .filter(JobRow.job_id == job_id, JobRow.status == RowStatus.FAILED.value)
        .all()
    )
    for row in failed_rows:
        if row.retry_count < 5:  # Max 5 retries
            row.status = RowStatus.PENDING.value
    db.commit()

    background_tasks.add_task(_run_job, job_id)
    return {"message": "Job resumed", "job_id": job_id}


@app.post("/api/jobs/{job_id}/pause")
def pause_job_endpoint(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.PROCESSING.value:
        raise HTTPException(status_code=400, detail="Job is not currently processing")
    pause_job(job_id)
    return {"message": "Pause signal sent", "job_id": job_id}


@app.get("/api/jobs/{job_id}/download")
def download_result(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.result_filename:
        raise HTTPException(status_code=404, detail="Result file not ready yet")
    path = os.path.join(settings.result_dir, job.result_filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Result file not found on disk")
    return FileResponse(
        path,
        filename=f"QC_Result_{job.original_filename}",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if is_job_active(job_id):
        pause_job(job_id)

    # Collect screenshot URLs from job rows for cache cleanup
    job_rows = db.query(JobRow).filter(JobRow.job_id == job_id).all()
    screenshot_urls = set()
    for row in job_rows:
        if row.screenshot_url:
            screenshot_urls.add(row.screenshot_url.strip())

    # Clean up uploaded file
    if job.filename:
        upload_path = os.path.join(settings.upload_dir, job.filename)
        if os.path.exists(upload_path):
            os.remove(upload_path)
            logger.info(f"Deleted upload: {upload_path}")

    # Clean up result file
    if job.result_filename and job.result_filename != "gsheet_written":
        result_path = os.path.join(settings.result_dir, job.result_filename)
        if os.path.exists(result_path):
            os.remove(result_path)
            logger.info(f"Deleted result: {result_path}")

    # Clean up cached images for this job's screenshot URLs
    import hashlib
    cache_dir = "./data/image_cache"
    cleaned_cache = 0
    for url in screenshot_urls:
        cache_key = hashlib.sha256(url.encode()).hexdigest()
        cache_file = os.path.join(cache_dir, cache_key)
        if os.path.exists(cache_file):
            os.remove(cache_file)
            cleaned_cache += 1
    if cleaned_cache:
        logger.info(f"Deleted {cleaned_cache} cached images for job {job_id}")

    # Delete DB rows + job (cascade should handle rows, but be explicit)
    db.query(JobRow).filter(JobRow.job_id == job_id).delete()
    db.delete(job)
    db.commit()
    return {"message": "Job deleted", "cleaned_files": cleaned_cache + (1 if job.filename else 0) + (1 if job.result_filename else 0)}


# ── Admin Settings ───────────────────────────────────────────────────────

@app.get("/api/admin/settings", response_model=List[AdminSettingResponse])
def get_admin_settings(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return db.query(AdminSetting).all()


@app.put("/api/admin/settings")
def update_admin_setting(
    req: AdminSettingUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    setting = db.query(AdminSetting).filter(AdminSetting.key == req.key).first()
    if setting:
        setting.value = req.value
    else:
        db.add(AdminSetting(key=req.key, value=req.value))
    db.commit()
    return {"message": "Setting updated"}


@app.post("/api/admin/clear-cache")
def admin_clear_cache(admin: User = Depends(require_admin)):
    clear_image_cache()
    return {"message": "Image cache cleared"}


@app.post("/api/admin/google-credentials")
async def upload_google_credentials(
    file: UploadFile = File(...),
    admin: User = Depends(require_admin),
):
    if not file.filename.endswith(".json"):
        raise HTTPException(status_code=400, detail="Only .json files are supported")
    content = await file.read()
    import json
    try:
        json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON file")
    with open(settings.google_credentials_path, "wb") as f:
        f.write(content)
    reset_client()
    return {"message": "Google credentials uploaded successfully"}


@app.delete("/api/admin/google-credentials")
def delete_google_credentials(admin: User = Depends(require_admin)):
    if os.path.exists(settings.google_credentials_path):
        os.remove(settings.google_credentials_path)
    reset_client()
    return {"message": "Google credentials removed"}


# ── Stats ────────────────────────────────────────────────────────────────

@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db)):
    total_jobs = db.query(Job).count()
    completed_jobs = db.query(Job).filter(Job.status == JobStatus.COMPLETED.value).count()
    total_rows_processed = sum(
        j.processed_rows for j in db.query(Job).all()
    )
    return {
        "total_jobs": total_jobs,
        "completed_jobs": completed_jobs,
        "total_rows_processed": total_rows_processed,
    }
