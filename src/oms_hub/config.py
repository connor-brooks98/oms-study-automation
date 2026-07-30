import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _normalize_hostname(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower().rstrip(".")
    if not re.fullmatch(
        r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
        r"[a-z]{2,63}",
        normalized,
    ):
        raise ValueError(f"{field_name} must be a hostname without a scheme or port")
    return normalized


def _validate_secret_key_name(value: str) -> str:
    normalized = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", normalized):
        raise ValueError("credential key name contains unsupported characters")
    return normalized


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OMS_HUB_", env_file=".env", extra="ignore")

    data_dir: Path = Path(r"C:\ProgramData\OMSStudyHub")
    database_url: str = "sqlite:///C:/ProgramData/OMSStudyHub/hub.db"
    timezone: str = "America/New_York"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = Field(default=8765, ge=1024, le=65535)
    public_hostname: str | None = None
    cloudflare_access_issuer: str | None = None
    cloudflare_access_audience: str | None = None
    cloudflare_access_allowed_email: str | None = None
    allow_local_access: bool = True
    study_root: Path = Path(r"%USERPROFILE%\Documents\OMS II")
    icloud_staging_root: Path | None = None
    office_timeout_seconds: int = Field(default=180, ge=30, le=600)
    max_upload_file_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=1,
    )
    max_upload_batch_bytes: int = Field(
        default=500 * 1024 * 1024,
        ge=1,
    )
    upload_session_hours: int = Field(default=24, ge=1, le=168)
    transcript_prompt_path: Path = Path(
        r"C:\Users\conbr\Documents\Main Vault\Anki AI Prompts\Transcript Cleaning.md"
    )
    transcript_prompt_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    transcript_min_clean_ratio: float = Field(default=0.60, ge=0.1, le=1.0)
    transcript_max_clean_ratio: float = Field(default=1.25, ge=1.0, le=2.0)
    openai_model: str = "gpt-5.2"
    openai_input_usd_per_million: float = Field(default=2.50, ge=0)
    openai_output_usd_per_million: float = Field(default=15.00, ge=0)
    generation_timeout_seconds: int = Field(default=180, ge=30, le=600)
    anki_enabled: bool = False
    anki_data_dir: Path | None = None
    anki_agent_hostname: str | None = None
    anki_agent_token_key: str = "anki-agent-token"
    anki_agent_heartbeat_max_age_seconds: int = Field(
        default=24 * 60 * 60,
        ge=60,
        le=30 * 24 * 60 * 60,
    )
    anki_agent_max_request_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=256,
        le=500 * 1024 * 1024,
    )
    anki_snapshot_max_age_hours: int = Field(default=48, ge=1, le=24 * 30)
    anki_worker_poll_seconds: float = Field(default=5.0, ge=0.5, le=60.0)
    anki_embedding_model: str = Field(
        default="BAAI/bge-small-en-v1.5",
        min_length=1,
        max_length=200,
    )
    anki_focused_retrieval_limit: int = Field(default=200, ge=1, le=5_000)
    anki_global_retrieval_limit: int = Field(default=50, ge=1, le=1_000)
    anki_semantic_model: str = Field(
        default="voyage-4-large",
        min_length=1,
        max_length=200,
    )
    anki_semantic_dimensions: int = Field(default=1024, ge=1, le=16_384)
    anki_semantic_min_coverage: float = Field(default=0.995, ge=0.0, le=1.0)
    anki_semantic_batch_size: int = Field(default=128, ge=1, le=1_000)
    anki_semantic_query_cache_size: int = Field(
        default=512,
        ge=1,
        le=100_000,
    )
    anki_connect_url: str = "http://127.0.0.1:8765"
    anki_image_low_estimate_usd: float = Field(default=0.0, ge=0)
    anki_image_medium_estimate_usd: float = Field(default=0.0, ge=0)
    anki_image_high_estimate_usd: float = Field(default=0.0, ge=0)

    @property
    def resolved_anki_data_dir(self) -> Path:
        return self.anki_data_dir if self.anki_data_dir is not None else self.data_dir / "anki"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("public_hostname")
    @classmethod
    def normalize_public_hostname(cls, value: str | None) -> str | None:
        return _normalize_hostname(value, "public_hostname")

    @field_validator("anki_agent_hostname")
    @classmethod
    def normalize_anki_agent_hostname(cls, value: str | None) -> str | None:
        return _normalize_hostname(value, "anki_agent_hostname")

    @field_validator("anki_agent_token_key")
    @classmethod
    def validate_anki_agent_token_key(cls, value: str) -> str:
        return _validate_secret_key_name(value)

    @field_validator("anki_connect_url")
    @classmethod
    def validate_anki_connect_url(cls, value: str) -> str:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port != 8765
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "AnkiConnect must use the loopback URL "
                "http://127.0.0.1:8765 or http://localhost:8765"
            )
        return f"http://{parsed.hostname}:8765"

    @field_validator("cloudflare_access_issuer")
    @classmethod
    def normalize_access_issuer(cls, value: str | None) -> str | None:
        if not value:
            return None
        normalized = value.strip().rstrip("/")
        if not re.fullmatch(
            r"https://[a-z0-9][a-z0-9-]*\.cloudflareaccess\.com",
            normalized,
            flags=re.IGNORECASE,
        ):
            raise ValueError("Cloudflare Access issuer must be the HTTPS team domain")
        return normalized


@lru_cache
def get_settings() -> Settings:
    return Settings()
