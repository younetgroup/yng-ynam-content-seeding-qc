import os
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean,
    DateTime, Text, ForeignKey, Enum as SAEnum
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime, timezone
import enum

from .config import settings

os.makedirs(os.path.dirname(settings.database_url.replace("sqlite:///", "")) or ".", exist_ok=True)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class RowStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class AdminSetting(Base):
    __tablename__ = "admin_settings"
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class Job(Base):
    __tablename__ = "jobs"
    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String(500), nullable=False)
    original_filename = Column(String(500), nullable=False)
    job_name = Column(String(500), nullable=True)
    status = Column(String(20), default=JobStatus.PENDING.value)
    total_rows = Column(Integer, default=0)
    processed_rows = Column(Integer, default=0)
    matched_rows = Column(Integer, default=0)
    failed_rows = Column(Integer, default=0)
    header_line = Column(Integer, default=1)
    comment_column = Column(String(10), default="K")
    screenshot_column = Column(String(10), default="O")
    result_column = Column(String(10), default="P")
    ocr_dump_column = Column(String(10), nullable=True)
    parallel_workers = Column(Integer, default=2)
    match_threshold = Column(Float, default=80.0)
    sheet_name = Column(String(255), nullable=True)
    start_row = Column(Integer, nullable=True)
    end_row = Column(Integer, nullable=True)
    gsheet_url = Column(Text, nullable=True)  # If set, this is a Google Sheet job
    result_filename = Column(String(500), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
    rows = relationship("JobRow", back_populates="job", cascade="all, delete-orphan")


class JobRow(Base):
    __tablename__ = "job_rows"
    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    row_number = Column(Integer, nullable=False)
    comment_text = Column(Text, nullable=True)
    screenshot_url = Column(Text, nullable=True)
    ocr_text = Column(Text, nullable=True)
    match_score = Column(Float, nullable=True)
    match_result = Column(Integer, nullable=True)  # 1 or 0
    status = Column(String(20), default=RowStatus.PENDING.value)
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0)
    job = relationship("Job", back_populates="rows")


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
