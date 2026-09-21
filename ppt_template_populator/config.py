from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import tempfile
from dotenv import load_dotenv
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _directory_writable(directory: Path) -> bool:
    probe: Path | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".write-test-", delete=False) as handle:
            probe = Path(handle.name)
        return True
    except OSError:
        return False
    finally:
        if probe is not None:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")
    watsonx_api_key: SecretStr | None = Field(default=None, alias="WATSONX_API_KEY")
    watsonx_url: str = Field(default="https://us-south.ml.cloud.ibm.com", alias="WATSONX_URL")
    watsonx_project_id: str | None = Field(default=None, alias="WATSONX_PROJECT_ID")
    watsonx_model_id: str | None = Field(default=None, alias="WATSONX_MODEL_ID")
    watsonx_embedding_model_id: str | None = Field(default=None, alias="WATSONX_EMBEDDING_MODEL_ID")
    elasticsearch_url: str = Field(default="http://localhost:9200", alias="ELASTICSEARCH_URL")
    elasticsearch_username: str | None = Field(default=None, alias="ELASTICSEARCH_USERNAME")
    elasticsearch_password: SecretStr | None = Field(default=None, alias="ELASTICSEARCH_PASSWORD")
    minio_endpoint: str = Field(default="localhost:9000", alias="MINIO_ENDPOINT")
    minio_access_key: str | None = Field(default=None, alias="MINIO_ACCESS_KEY")
    minio_secret_key: SecretStr | None = Field(default=None, alias="MINIO_SECRET_KEY")
    minio_secure: bool = Field(default=False, alias="MINIO_SECURE")
    minio_templates_bucket: str = Field(default="ppt-templates", alias="MINIO_TEMPLATES_BUCKET")
    minio_uploads_bucket: str = Field(default="ppt-uploads", alias="MINIO_UPLOADS_BUCKET")
    max_upload_mb: int = Field(default=25, alias="MAX_UPLOAD_MB", ge=1, le=200)
    max_total_upload_mb: int = Field(default=100, alias="MAX_TOTAL_UPLOAD_MB", ge=1, le=1000)
    request_timeout_seconds: int = Field(default=120, alias="REQUEST_TIMEOUT_SECONDS", ge=10, le=1800)
    template_selection_confidence: float = Field(default=0.60, alias="TEMPLATE_SELECTION_CONFIDENCE", ge=0, le=1)
    max_required_targets_per_chunk: int = Field(default=12, alias="MAX_REQUIRED_TARGETS_PER_CHUNK", ge=1, le=100)
    templates_dir: Path = BASE_DIR / "templates"
    generated_dir: Path = BASE_DIR / "generated"
    data_dir: Path = BASE_DIR / "data"

    def ensure_directories(self) -> None:
        for directory in (self.templates_dir, self.data_dir):
            directory.mkdir(parents=True, exist_ok=True)
        if not _directory_writable(self.generated_dir):
            fallback = BASE_DIR.parent / "runtime_generated"
            if not _directory_writable(fallback):
                raise PermissionError("No writable presentation output directory is available.")
            self.generated_dir = fallback

    def watsonx_missing(self) -> list[str]:
        values = {"WATSONX_API_KEY": self.watsonx_api_key.get_secret_value() if self.watsonx_api_key else "", "WATSONX_URL": self.watsonx_url, "WATSONX_PROJECT_ID": self.watsonx_project_id or ""}
        return [name for name, value in values.items() if not value.strip()]

    def minio_missing(self) -> list[str]:
        values = {"MINIO_ENDPOINT": self.minio_endpoint, "MINIO_ACCESS_KEY": self.minio_access_key or "", "MINIO_SECRET_KEY": self.minio_secret_key.get_secret_value() if self.minio_secret_key else ""}
        return [name for name, value in values.items() if not value.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
