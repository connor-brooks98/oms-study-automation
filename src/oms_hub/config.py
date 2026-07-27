import re
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("public_hostname")
    @classmethod
    def normalize_public_hostname(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower().rstrip(".")
        if not re.fullmatch(
            r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
            r"[a-z]{2,63}",
            normalized,
        ):
            raise ValueError("public_hostname must be a hostname without a scheme or port")
        return normalized

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
