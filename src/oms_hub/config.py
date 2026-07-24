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
    outlook_client_id: str | None = None
    outlook_tenant: str = "organizations"
    outlook_sync_days_ahead: int = Field(default=14, ge=1, le=90)
    canvas_base_url: str = "https://lmunet.instructure.com"
    canvas_inbox: Path = Path(r"%USERPROFILE%\Downloads\OMSStudyHub\CanvasInbox")
    revision_root: Path = Path(r"C:\ProgramData\OMSStudyHub\artifacts\revisions")
    study_root: Path = Path(r"%USERPROFILE%\Documents\OMS II")
    icloud_staging_root: Path | None = None
    canvas_auto_process: bool = False
    canvas_scan_minutes: int = Field(default=30, ge=30, le=30)
    office_timeout_seconds: int = Field(default=180, ge=30, le=600)
    max_ingest_bytes: int = Field(default=250 * 1024 * 1024, ge=1)
    panopto_tenant_url: str = "https://lmunet.hosted.panopto.com"
    panopto_revision_root: Path = Path(
        r"C:\ProgramData\OMSStudyHub\artifacts\panopto\revisions"
    )
    panopto_inbox: Path = Path(
        r"%USERPROFILE%\Downloads\OMSStudyHub\PanoptoInbox"
    )
    panopto_quarantine_root: Path = Path(
        r"C:\ProgramData\OMSStudyHub\quarantine\panopto"
    )
    panopto_acceptance_session_id: str = "8796399e-393c-4256-b6e4-b48f0150d156"
    panopto_poll_minutes: int = Field(default=15, ge=15, le=15)
    panopto_poll_start: str = "09:20"
    panopto_poll_end: str = "19:00"
    panopto_max_caption_bytes: int = Field(default=5 * 1024 * 1024, ge=1)
    transcript_prompt_path: Path = Path(
        r"C:\Users\conbr\Documents\Main Vault\Anki AI Prompts\Transcript Cleaning.md"
    )
    transcript_min_clean_ratio: float = Field(default=0.60, ge=0.1, le=1.0)
    transcript_max_clean_ratio: float = Field(default=1.25, ge=1.0, le=2.0)
    openai_model: str = "gpt-5.6-terra"
    openai_input_usd_per_million: float = Field(default=2.50, ge=0)
    openai_output_usd_per_million: float = Field(default=15.00, ge=0)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
