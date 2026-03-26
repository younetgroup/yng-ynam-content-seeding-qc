from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    database_url: str = "sqlite:///./data/content_qc.db"
    secret_key: str = "younet-content-qc-secret-key-change-in-production-2026"
    default_admin_email: str = "it@younetgroup.com"
    default_admin_password: str = "YouNet@2026"
    max_workers: int = 2
    match_threshold: int = 80
    access_token_expire_minutes: int = 1440  # 24 hours
    upload_dir: str = "./data/uploads"
    result_dir: str = "./data/results"
    google_credentials_path: str = "./data/google_credentials.json"

    class Config:
        env_file = ".env"


settings = Settings()
