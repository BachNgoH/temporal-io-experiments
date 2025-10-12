"""Application configuration."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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


settings = Settings()
