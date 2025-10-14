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


    # CAPTCHA / Gemini / Vertex AI
    # When using Vertex AI (preferred), set CAPTCHA_USE_VERTEX=true and provide GCP project/region.
    # Otherwise, set GEMINI_API_KEY and CAPTCHA_MODEL for direct Gemini API usage.
    CAPTCHA_USE_VERTEX: bool = True
    GCP_PROJECT_ID: str | None = None
    GCP_REGION: str = "asia-southeast1"
    CAPTCHA_MODEL: str = "gemini-2.5-flash"
    GOOGLE_APPLICATION_CREDENTIALS: str = "/app/credentials/vertex-ai-sa-key.json"
    GEMINI_API_KEY: str | None = None


settings = Settings()
