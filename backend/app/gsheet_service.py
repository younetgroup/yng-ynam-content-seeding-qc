"""
Google Sheets integration: read rows and write results directly to Google Sheets.
Requires a service account credentials JSON uploaded via Admin.
"""
import os
import re
import logging
from typing import List, Optional, Tuple

import gspread
from google.oauth2.service_account import Credentials

from .config import settings

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

_client: Optional[gspread.Client] = None


def is_credentials_configured() -> bool:
    return os.path.exists(settings.google_credentials_path)


def get_gspread_client() -> gspread.Client:
    global _client
    if _client is None:
        if not is_credentials_configured():
            raise RuntimeError("Google credentials not configured. Upload via Admin.")
        creds = Credentials.from_service_account_file(
            settings.google_credentials_path, scopes=SCOPES
        )
        _client = gspread.authorize(creds)
    return _client


def reset_client():
    """Reset cached client (e.g. after new credentials upload)."""
    global _client
    _client = None


def parse_gsheet_url(url: str) -> Tuple[str, Optional[int]]:
    """
    Extract spreadsheet ID and optional gid (sheet index) from URL.
    Supports:
      https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit#gid=SHEET_GID
      https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit
    """
    m = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]+)', url)
    if not m:
        raise ValueError("Invalid Google Sheet URL")
    spreadsheet_id = m.group(1)

    gid = None
    gid_m = re.search(r'gid=(\d+)', url)
    if gid_m:
        gid = int(gid_m.group(1))

    return spreadsheet_id, gid


def get_sheet_info(url: str) -> dict:
    """Get sheet names and preview rows from a Google Sheet."""
    client = get_gspread_client()
    spreadsheet_id, gid = parse_gsheet_url(url)
    spreadsheet = client.open_by_key(spreadsheet_id)

    sheet_names = [ws.title for ws in spreadsheet.worksheets()]

    # Pick sheet by gid or default to first
    worksheet = spreadsheet.worksheets()[0]
    if gid is not None:
        for ws in spreadsheet.worksheets():
            if ws.id == gid:
                worksheet = ws
                break

    # Get first 3 rows for preview
    all_values = worksheet.get_all_values()
    preview_rows = []
    for i, row in enumerate(all_values[:3]):
        preview_rows.append({"row_number": i + 1, "cells": row})

    max_col = max(len(r) for r in all_values[:3]) if all_values else 1
    from openpyxl.utils import get_column_letter
    col_letters = [get_column_letter(i + 1) for i in range(max_col)]

    return {
        "sheet_names": sheet_names,
        "active_sheet": worksheet.title,
        "rows": preview_rows,
        "total_columns": max_col,
        "column_letters": col_letters,
        "total_rows": len(all_values),
    }


def read_gsheet_rows(
    url: str,
    sheet_name: str,
    header_line: int,
    comment_col_idx: int,
    screenshot_col_idx: int,
    start_row: Optional[int] = None,
    end_row: Optional[int] = None,
) -> List[dict]:
    """Read rows from Google Sheet for job processing."""
    client = get_gspread_client()
    spreadsheet_id, _ = parse_gsheet_url(url)
    spreadsheet = client.open_by_key(spreadsheet_id)
    worksheet = spreadsheet.worksheet(sheet_name)

    all_values = worksheet.get_all_values()
    data_start = header_line  # 0-based index for data start (header_line is 1-based)

    effective_start = max(data_start, (start_row - 1) if start_row else data_start)
    effective_end = min(len(all_values), end_row if end_row else len(all_values))

    rows = []
    # Track last non-empty screenshot URL — rows without one inherit from above
    last_screenshot = ""

    # Pre-scan rows before effective_start to find the latest screenshot URL
    for i in range(data_start, effective_start):
        if i < len(all_values):
            row = all_values[i]
            ss = row[screenshot_col_idx].strip() if screenshot_col_idx < len(row) else ""
            if ss and ss.lower() != "none":
                last_screenshot = ss

    for i in range(effective_start, effective_end):
        row = all_values[i]
        row_num = i + 1  # 1-based Excel-style row number
        comment = row[comment_col_idx].strip() if comment_col_idx < len(row) else ""
        screenshot = row[screenshot_col_idx].strip() if screenshot_col_idx < len(row) else ""

        if comment.lower() == "none":
            comment = ""
        if screenshot.lower() == "none":
            screenshot = ""

        # Update last_screenshot if this row has one, otherwise inherit
        if screenshot:
            last_screenshot = screenshot
        else:
            screenshot = last_screenshot

        # Skip empty comment rows — nothing to verify
        if not comment:
            continue

        rows.append({
            "row_number": row_num,
            "comment_text": comment,
            "screenshot_url": screenshot,
        })

    return rows


def write_gsheet_results(
    url: str,
    sheet_name: str,
    result_col_idx: int,
    ocr_dump_col_idx: Optional[int],
    header_line: int,
    results: List[dict],
):
    """
    Write QC results back to Google Sheet.
    results: list of {row_number, match_result, ocr_text}
    """
    client = get_gspread_client()
    spreadsheet_id, _ = parse_gsheet_url(url)
    spreadsheet = client.open_by_key(spreadsheet_id)
    worksheet = spreadsheet.worksheet(sheet_name)

    # Write header
    worksheet.update_cell(header_line, result_col_idx + 1, "QC Result")
    if ocr_dump_col_idx is not None:
        worksheet.update_cell(header_line, ocr_dump_col_idx + 1, "OCR Text")

    # Batch update for efficiency
    result_cells = []
    ocr_cells = []

    for r in results:
        row_num = r["row_number"]
        val = r.get("match_result")
        if val is not None:
            result_cells.append(gspread.Cell(row_num, result_col_idx + 1, val))
        elif r.get("status") == "failed":
            result_cells.append(gspread.Cell(row_num, result_col_idx + 1, "ERROR"))

        if ocr_dump_col_idx is not None and r.get("ocr_text"):
            ocr_cells.append(gspread.Cell(row_num, ocr_dump_col_idx + 1, r["ocr_text"]))

    if result_cells:
        worksheet.update_cells(result_cells)
    if ocr_cells:
        worksheet.update_cells(ocr_cells)

    logger.info(f"Wrote {len(result_cells)} results to Google Sheet")
