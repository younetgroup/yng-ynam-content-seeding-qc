from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    email: str
    is_admin: bool


class JobCreateRequest(BaseModel):
    header_line: int = 1
    comment_column: str = "K"
    screenshot_column: str = "O"
    result_column: str = "P"
    ocr_dump_column: Optional[str] = None
    parallel_workers: int = 2
    match_threshold: Optional[float] = None  # Will use admin default if None
    sheet_name: Optional[str] = None  # Excel sheet name, None = active sheet
    start_row: Optional[int] = None  # Process from this row (inclusive)
    end_row: Optional[int] = None  # Process to this row (inclusive)
    gsheet_url: Optional[str] = None  # Google Sheet URL (if using GSheet mode)
    job_name: Optional[str] = None


class JobRenameRequest(BaseModel):
    job_name: str


class JobResponse(BaseModel):
    id: int
    original_filename: str
    job_name: Optional[str] = None
    filename: Optional[str] = None
    status: str
    total_rows: int
    processed_rows: int
    matched_rows: int
    failed_rows: int
    header_line: int
    comment_column: str
    screenshot_column: str
    result_column: str
    ocr_dump_column: Optional[str]
    parallel_workers: int
    match_threshold: float
    result_filename: Optional[str]
    gsheet_url: Optional[str] = None
    error_message: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class JobRowResponse(BaseModel):
    id: int
    row_number: int
    comment_text: Optional[str]
    screenshot_url: Optional[str]
    ocr_text: Optional[str]
    match_score: Optional[float]
    match_result: Optional[int]
    status: str
    error_message: Optional[str]
    retry_count: int

    class Config:
        from_attributes = True


class PreviewRow(BaseModel):
    row_number: int
    cells: List[Optional[str]]


class FilePreviewResponse(BaseModel):
    filename: str
    rows: List[PreviewRow]
    total_columns: int
    column_letters: List[str]
    sheet_names: List[str] = []


class AdminSettingUpdate(BaseModel):
    key: str
    value: str


class AdminSettingResponse(BaseModel):
    key: str
    value: str
    description: Optional[str]

    class Config:
        from_attributes = True


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
