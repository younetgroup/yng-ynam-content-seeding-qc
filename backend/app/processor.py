"""
Excel processor: reads uploaded files, manages OCR jobs, writes results.
Handles grouping of rows by screenshot URL and parallel processing.
"""
import os
import asyncio
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone

import openpyxl
from openpyxl.utils import get_column_letter, column_index_from_string
from sqlalchemy.orm import Session

from .database import Job, JobRow, JobStatus, RowStatus, SessionLocal
from .ocr_service import download_image, run_ocr_async, _cache_key
from .matcher import match_comment_in_ocr

logger = logging.getLogger(__name__)

# Track active jobs so we can pause/cancel
_active_jobs: Dict[int, bool] = {}  # job_id -> should_continue

# Global download semaphore — limits concurrent downloads to avoid throttling
# Google Drive starts returning 429 at ~10 concurrent requests
_DOWNLOAD_SEMAPHORE = None  # Created lazily (needs event loop)
_MAX_CONCURRENT_DOWNLOADS = 3

# Delay between groups to avoid rate limiting (seconds)
_GROUP_DELAY = 0.5


def col_letter_to_index(letter: str) -> int:
    """Convert column letter (A, B, ..., Z, AA, ...) to 0-based index."""
    return column_index_from_string(letter.upper()) - 1


def read_excel_preview(filepath: str, num_rows: int = 3, sheet_name: str = None) -> dict:
    """Read first N rows of Excel file for preview/header selection."""
    wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    sheet_names = wb.sheetnames
    ws = wb[sheet_name] if sheet_name and sheet_name in sheet_names else wb.active
    rows = []
    max_col = 0  # compute from actual row data (ws.max_column unreliable in read_only)

    for i, row in enumerate(ws.iter_rows(min_row=1, max_row=num_rows, values_only=True)):
        cells = [str(c) if c is not None else "" for c in row]
        rows.append({"row_number": i + 1, "cells": cells})
        max_col = max(max_col, len(cells))

    max_col = max(max_col, 1)
    col_letters = [get_column_letter(i + 1) for i in range(max_col)]
    wb.close()

    return {
        "rows": rows,
        "total_columns": max_col,
        "column_letters": col_letters,
        "sheet_names": sheet_names,
    }


def parse_excel_for_job(
    filepath: str,
    header_line: int,
    comment_col: str,
    screenshot_col: str,
    sheet_name: str = None,
    start_row: int = None,
    end_row: int = None,
) -> List[dict]:
    """
    Parse Excel file and extract rows with comment + screenshot URL.
    Returns list of dicts: {row_number, comment_text, screenshot_url}
    Supports optional start_row/end_row to limit processing range.
    """
    wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active

    comment_idx = col_letter_to_index(comment_col)
    screenshot_idx = col_letter_to_index(screenshot_col)

    rows = []
    data_start = header_line + 1
    # Apply row range if specified
    effective_start = max(data_start, start_row) if start_row else data_start

    # Track the last non-empty screenshot URL so rows without one inherit it
    last_screenshot = ""

    for i, row in enumerate(ws.iter_rows(min_row=data_start, values_only=True)):
        row_num = data_start + i
        if row_num < effective_start:
            # Still track screenshot URLs even before effective_start
            raw_ss = str(row[screenshot_idx]).strip() if screenshot_idx < len(row) and row[screenshot_idx] else ""
            if raw_ss and raw_ss.lower() != "none":
                last_screenshot = raw_ss
            continue
        if end_row and row_num > end_row:
            break

        comment = str(row[comment_idx]).strip() if comment_idx < len(row) and row[comment_idx] else ""
        screenshot = str(row[screenshot_idx]).strip() if screenshot_idx < len(row) and row[screenshot_idx] else ""

        # Clean up "None" strings
        if comment.lower() == "none":
            comment = ""
        if screenshot.lower() == "none":
            screenshot = ""

        # Update last_screenshot if this row has one
        if screenshot:
            last_screenshot = screenshot
        else:
            # Inherit from nearest row above that had a screenshot URL
            screenshot = last_screenshot

        # Skip completely empty rows (no comment text)
        if not comment:
            continue

        rows.append({
            "row_number": row_num,
            "comment_text": comment,
            "screenshot_url": screenshot,
        })

    wb.close()
    return rows


def write_results_to_excel(
    source_path: str,
    result_path: str,
    job: Job,
    rows: List[JobRow],
    header_line: int,
):
    """Write match results back to a copy of the Excel file."""
    wb = openpyxl.load_workbook(source_path)
    sheet_name = getattr(job, 'sheet_name', None)
    ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active

    result_idx = col_letter_to_index(job.result_column)
    ocr_dump_idx = col_letter_to_index(job.ocr_dump_column) if job.ocr_dump_column else None

    # Write header
    result_header_cell = ws.cell(row=header_line, column=result_idx + 1)
    result_header_cell.value = "QC Result"
    result_header_cell.font = openpyxl.styles.Font(bold=True)

    if ocr_dump_idx is not None:
        ocr_header_cell = ws.cell(row=header_line, column=ocr_dump_idx + 1)
        ocr_header_cell.value = "OCR Text"
        ocr_header_cell.font = openpyxl.styles.Font(bold=True)

    # Build row lookup
    row_lookup = {r.row_number: r for r in rows}

    for row_num, job_row in row_lookup.items():
        # Write result (1 or 0)
        cell = ws.cell(row=row_num, column=result_idx + 1)
        if job_row.match_result is not None:
            cell.value = job_row.match_result
            # Color coding
            if job_row.match_result == 1:
                cell.font = openpyxl.styles.Font(color="008000", bold=True)  # Green
            else:
                cell.font = openpyxl.styles.Font(color="FF0000", bold=True)  # Red
        elif job_row.status == RowStatus.FAILED.value:
            cell.value = "ERROR"
            cell.font = openpyxl.styles.Font(color="FF0000", italic=True)

        # Write OCR text if column specified
        if ocr_dump_idx is not None and job_row.ocr_text:
            ws.cell(row=row_num, column=ocr_dump_idx + 1).value = job_row.ocr_text

    os.makedirs(os.path.dirname(result_path), exist_ok=True)
    wb.save(result_path)
    wb.close()


async def _process_screenshot_group(
    screenshot_url: str,
    group_rows: List[JobRow],
    threshold: float,
    db: Session,
    job_id: int,
) -> Tuple[int, int, int]:
    """
    Process a group of rows sharing the same screenshot URL.
    Downloads + OCRs the image once, then matches all comments.

    Returns (processed_count, matched_count, failed_count)
    """
    processed = 0
    matched = 0
    failed = 0

    try:
        # Download with rate limiting (semaphore limits concurrent downloads)
        global _DOWNLOAD_SEMAPHORE
        if _DOWNLOAD_SEMAPHORE is None:
            import asyncio
            _DOWNLOAD_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENT_DOWNLOADS)

        logger.info(f"Downloading image: {screenshot_url[:80]}...")
        async with _DOWNLOAD_SEMAPHORE:
            image_data = await download_image(screenshot_url)
        logger.info(f"Downloaded {len(image_data)} bytes, starting OCR...")
        ocr_text = await run_ocr_async(image_data, cache_key=_cache_key(screenshot_url))
        logger.info(f"OCR done, got {len(ocr_text)} chars")

        for row in group_rows:
            if not _active_jobs.get(job_id, False):
                break  # Job paused or cancelled

            try:
                row.ocr_text = ocr_text
                row.status = RowStatus.PROCESSING.value
                db.commit()

                is_match, score, strategy = match_comment_in_ocr(
                    row.comment_text, ocr_text, threshold
                )
                row.match_result = 1 if is_match else 0
                row.match_score = score
                row.status = RowStatus.COMPLETED.value
                row.error_message = None  # Clear stale errors from previous retries
                processed += 1
                if is_match:
                    matched += 1

                db.commit()

            except Exception as e:
                logger.error(f"Row {row.row_number} match error: {e}")
                row.status = RowStatus.FAILED.value
                row.error_message = str(e)[:500]
                row.retry_count += 1
                db.commit()
                failed += 1

    except Exception as e:
        logger.error(f"Screenshot group error ({screenshot_url}): {e}")
        for row in group_rows:
            row.status = RowStatus.FAILED.value
            row.error_message = f"Image download/OCR failed: {str(e)[:400]}"
            row.retry_count += 1
            failed += 1
        db.commit()

    return processed, matched, failed


async def process_job(job_id: int):
    """
    Main job processor. Groups rows by screenshot URL, processes SEQUENTIALLY
    to avoid SQLite concurrent-write deadlocks. Updates progress after each group.
    Supports resume — skips already-completed rows.
    """
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            logger.error(f"Job {job_id} not found")
            return

        _active_jobs[job_id] = True
        job.status = JobStatus.PROCESSING.value
        db.commit()

        # Get pending/failed rows (for resume support)
        pending_rows = (
            db.query(JobRow)
            .filter(
                JobRow.job_id == job_id,
                JobRow.status.in_([RowStatus.PENDING.value, RowStatus.FAILED.value]),
            )
            .all()
        )

        if not pending_rows:
            job.status = JobStatus.COMPLETED.value
            db.commit()
            _write_result_file(job, db)
            return

        # Group rows by screenshot URL
        groups: Dict[str, List[JobRow]] = {}
        for row in pending_rows:
            url = (row.screenshot_url or "").strip()
            if not url:
                row.status = RowStatus.SKIPPED.value
                row.match_result = 0
                row.error_message = "No screenshot URL"
                continue
            groups.setdefault(url, []).append(row)

        db.commit()

        # Process groups SEQUENTIALLY to avoid SQLite deadlocks
        group_count = 0
        for url, rows in groups.items():
            if not _active_jobs.get(job_id, False):
                break  # Job paused or cancelled

            # Rate limit: delay between groups to avoid Google Drive throttling
            if group_count > 0:
                await asyncio.sleep(_GROUP_DELAY)

            await _process_screenshot_group(
                url, rows, job.match_threshold, db, job_id
            )
            group_count += 1

            # Update progress after each group so polling API returns fresh data
            job.processed_rows = (
                db.query(JobRow)
                .filter(JobRow.job_id == job_id, JobRow.status == RowStatus.COMPLETED.value)
                .count()
            )
            job.matched_rows = (
                db.query(JobRow)
                .filter(JobRow.job_id == job_id, JobRow.match_result == 1)
                .count()
            )
            job.failed_rows = (
                db.query(JobRow)
                .filter(JobRow.job_id == job_id, JobRow.status == RowStatus.FAILED.value)
                .count()
            )
            db.commit()

        # Final status
        already_done = (
            db.query(JobRow)
            .filter(JobRow.job_id == job_id, JobRow.status == RowStatus.COMPLETED.value)
            .count()
        )
        already_failed = (
            db.query(JobRow)
            .filter(JobRow.job_id == job_id, JobRow.status == RowStatus.FAILED.value)
            .count()
        )
        already_matched = (
            db.query(JobRow)
            .filter(JobRow.job_id == job_id, JobRow.match_result == 1)
            .count()
        )

        job.processed_rows = already_done
        job.matched_rows = already_matched
        job.failed_rows = already_failed

        if not _active_jobs.get(job_id, False):
            job.status = JobStatus.PAUSED.value
        elif already_failed > 0 and already_done == 0:
            job.status = JobStatus.FAILED.value
        else:
            job.status = JobStatus.COMPLETED.value

        db.commit()

        # Write result file if any rows completed (even with some failures)
        if job.status in (JobStatus.COMPLETED.value, JobStatus.FAILED.value) and already_done > 0:
            _write_result_file(job, db)

    except Exception as e:
        logger.error(f"Job {job_id} critical error: {e}", exc_info=True)
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            if job:
                job.status = JobStatus.FAILED.value
                job.error_message = str(e)[:1000]
                db.commit()
        except Exception:
            pass
    finally:
        _active_jobs.pop(job_id, None)
        db.close()


def _write_result_file(job: Job, db: Session):
    """Generate the result Excel file, or write back to Google Sheet."""
    try:
        rows = db.query(JobRow).filter(JobRow.job_id == job.id).all()

        if job.gsheet_url:
            # Write results back to Google Sheet
            from .gsheet_service import write_gsheet_results
            results = []
            for r in rows:
                results.append({
                    "row_number": r.row_number,
                    "match_result": r.match_result,
                    "ocr_text": r.ocr_text,
                    "status": r.status,
                })
            result_col_idx = col_letter_to_index(job.result_column)
            ocr_dump_col_idx = col_letter_to_index(job.ocr_dump_column) if job.ocr_dump_column else None
            write_gsheet_results(
                job.gsheet_url, job.sheet_name or "Sheet1",
                result_col_idx, ocr_dump_col_idx, job.header_line, results,
            )
            job.result_filename = "gsheet_written"
            db.commit()
            logger.info(f"Results written to Google Sheet for job {job.id}")
        else:
            # Write to Excel file
            source_path = os.path.join("./data/uploads", job.filename)
            result_filename = f"result_{job.id}_{job.original_filename}"
            result_path = os.path.join("./data/results", result_filename)
            write_results_to_excel(source_path, result_path, job, rows, job.header_line)
            job.result_filename = result_filename
            db.commit()
            logger.info(f"Result file written: {result_path}")
    except Exception as e:
        logger.error(f"Failed to write results for job {job.id}: {e}")
        job.error_message = f"Result write failed: {str(e)[:500]}"
        db.commit()


def pause_job(job_id: int):
    """Signal a running job to pause."""
    _active_jobs[job_id] = False


def is_job_active(job_id: int) -> bool:
    return _active_jobs.get(job_id, False)
