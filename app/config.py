"""Application configuration."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Get the project root directory (parent of app/)
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"


class Settings(BaseSettings):
    """Application settings."""

    model_config = SettingsConfigDict(env_file=str(ENV_FILE), env_file_encoding="utf-8", extra="ignore")

    # Application
    app_name: str = "Temporal Task System"
    app_version: str = "0.1.0"
    debug: bool = False

    # Temporal
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "default-task-queue"

    # Optional: Temporal Cloud
    temporal_use_cloud: bool = False
    temporal_cert_path: str | None = None
    temporal_key_path: str | None = None

    # Webhooks (read directly from WEBHOOK_URL and WEBHOOK_SIGNING_SECRET)
    webhook_url: str = "http://localhost:8000/api/v1/internal/webhooks/ai-core"
    webhook_signing_secret: str = "test-webhook-secret-12345"


settings = Settings()
