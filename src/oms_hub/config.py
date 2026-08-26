import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


def _normalize_hostname(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower().rstrip(".")
    if not normalized:
        return None
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
    dashboard_port: int = Field(default=8787, ge=1024, le=65535)
    # Set by the Windows launcher for diagnostics only; these fields do not change
    # bind, auth, storage, or any other deployment behavior.
    deployment_root: Path | None = None
    build_revision: str | None = Field(default=None, max_length=128)
    build_tree: str | None = Field(default=None, max_length=128)
    public_hostname: str | None = None
    cloudflare_access_issuer: str | None = None
    cloudflare_access_audience: str | None = None
    cloudflare_access_allowed_email: str | None = None
    allow_local_access: bool = True
    study_root: Path = Path(r"%USERPROFILE%\Documents\OMS II")
    icloud_staging_root: Path | None = None
    office_timeout_seconds: int = Field(default=180, ge=30, le=600)
    document_parser_mode: Literal["legacy", "shadow", "anydoc"] = "shadow"
    max_upload_file_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=1,
    )
    max_upload_batch_bytes: int = Field(
        default=500 * 1024 * 1024,
        ge=1,
    )
    upload_session_hours: int = Field(default=24, ge=1, le=168)
    transcript_prompt_path: Path | None = None
    transcript_prompt_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    transcript_min_clean_ratio: float = Field(default=0.60, ge=0.1, le=1.0)
    transcript_max_clean_ratio: float = Field(default=1.25, ge=1.0, le=2.0)
    openai_model: str = "gpt-5.2"
    openai_input_usd_per_million: float = Field(default=2.50, ge=0)
    openai_output_usd_per_million: float = Field(default=15.00, ge=0)
    anki_enabled: bool = False
    anki_rehearsal_mode: Literal["off", "deterministic", "shadow"] = "off"
    anki_rehearsal_overlay_dir: Path | None = None
    anki_rehearsal_replay_dir: Path | None = None
    anki_rehearsal_egress_pins_json: str | None = None
    # Capture is enabled only by the isolated launcher after it has bound the
    # authorization document to the exact candidate/capsule/job identity.
    anki_rehearsal_capture_store: Path | None = None
    anki_rehearsal_capture_authorization_manifest: Path | None = None
    anki_rehearsal_capture_authorization_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    anki_rehearsal_capture_candidate_commit: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{40}$"
    )
    anki_rehearsal_capture_candidate_tree: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{40}$"
    )
    anki_rehearsal_capture_capsule_manifest_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    anki_rehearsal_capture_failed_job_id: str | None = None
    anki_data_dir: Path | None = None
    anki_agent_hostname: str | None = None
    anki_agent_token_key: str = "anki-agent-token"
    anki_agent_max_request_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=256,
        le=500 * 1024 * 1024,
    )
    anki_worker_poll_seconds: float = Field(default=5.0, ge=0.5, le=60.0)
    anki_worker_lease_seconds: int = Field(default=120, ge=3, le=3_600)
    anki_worker_max_stage_attempts: int = Field(default=3, ge=1, le=10)
    anki_prompt_directory: Path | None = None
    anki_prompt_git_sync: bool = False
    anki_prompt_git_timeout_seconds: int = Field(default=30, ge=1, le=300)
    anki_fixture_artifact_path: Path | None = None
    anki_card_centric_fixture_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
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
    voyage_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="VOYAGE_API_KEY",
        exclude=True,
        repr=False,
    )
    anki_connect_url: str = "http://127.0.0.1:8765"
    anki_executable_path: Path = Path(r"C:\Program Files\Anki\anki.exe")
    anki_startup_attempts: int = Field(default=20, ge=1, le=120)
    anki_startup_poll_seconds: float = Field(default=1.0, gt=0, le=30.0)

    @property
    def resolved_anki_data_dir(self) -> Path:
        return self.anki_data_dir if self.anki_data_dir is not None else self.data_dir / "anki"

    @property
    def voyage_api_key_value(self) -> str | None:
        if self.voyage_api_key is None:
            return None
        return self.voyage_api_key.get_secret_value().strip() or None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("transcript_prompt_sha256", mode="before")
    @classmethod
    def blank_transcript_prompt_sha256_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
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
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("AnkiConnect must use a loopback URL with a valid port") from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or port is None
            or not 1024 <= port <= 65535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "AnkiConnect must use an HTTP loopback URL with an explicit "
                "port from 1024 through 65535"
            )
        return f"http://{parsed.hostname}:{port}"

    @model_validator(mode="after")
    def validate_local_service_ports(self) -> Self:
        capture_values = (
            self.anki_rehearsal_capture_store,
            self.anki_rehearsal_capture_authorization_manifest,
            self.anki_rehearsal_capture_authorization_sha256,
            self.anki_rehearsal_capture_candidate_commit,
            self.anki_rehearsal_capture_candidate_tree,
            self.anki_rehearsal_capture_capsule_manifest_sha256,
            self.anki_rehearsal_capture_failed_job_id,
        )
        if any(value is not None for value in capture_values):
            if self.anki_rehearsal_mode != "shadow" or any(
                value is None for value in capture_values
            ):
                raise ValueError("capture requires complete shadow-only authorization settings")
            assert self.anki_rehearsal_capture_store is not None
            if not self.anki_rehearsal_capture_store.is_absolute():
                raise ValueError("capture store must be an absolute private path")
        anki_connect_port = urlsplit(self.anki_connect_url).port
        if self.anki_enabled and anki_connect_port == self.dashboard_port:
            raise ValueError(
                "Study Hub and AnkiConnect cannot share port "
                f"{self.dashboard_port}; set OMS_HUB_DASHBOARD_PORT "
                "to a distinct loopback port such as 8787"
            )
        if self.anki_rehearsal_mode != "off":
            if not self.anki_enabled:
                raise ValueError("Anki rehearsal mode requires Anki to be enabled")
            if self.dashboard_host not in {"127.0.0.1", "localhost"}:
                raise ValueError("Anki rehearsal must bind Study Hub to loopback")
            if self.public_hostname is not None or self.anki_agent_hostname is not None:
                raise ValueError("Anki rehearsal cannot expose public or agent hostnames")
            if self.anki_prompt_git_sync:
                raise ValueError("Anki rehearsal cannot synchronize prompts from Git")
            if self.anki_rehearsal_overlay_dir is None or self.anki_rehearsal_replay_dir is None:
                raise ValueError("Anki rehearsal requires overlay and replay directories")
            overlay = self.anki_rehearsal_overlay_dir.resolve()
            controlled_paths = {
                "data directory": self.data_dir.resolve(),
                "Anki data directory": self.resolved_anki_data_dir.resolve(),
                "replay directory": self.anki_rehearsal_replay_dir.resolve(),
                "study root": self.study_root.resolve(),
            }
            if self.icloud_staging_root is None:
                raise ValueError("Anki rehearsal requires an iCloud staging root")
            controlled_paths["iCloud staging root"] = self.icloud_staging_root.resolve()
            database_path = make_url(self.database_url).database
            if make_url(self.database_url).drivername != "sqlite" or not database_path:
                raise ValueError("Anki rehearsal requires a SQLite overlay database")
            controlled_paths["database"] = Path(database_path).resolve()
            for label, path in controlled_paths.items():
                if not path.is_relative_to(overlay):
                    raise ValueError(f"Anki rehearsal {label} must be inside the overlay")
            read_only_inputs = {
                "Anki prompt directory": self.anki_prompt_directory,
                "Anki fixture artifact": self.anki_fixture_artifact_path,
                "transcript prompt": self.transcript_prompt_path,
            }
            for label, input_path in read_only_inputs.items():
                if input_path is not None and not input_path.resolve().is_relative_to(overlay):
                    raise ValueError(
                        f"Anki rehearsal {label} must be inside the materialized overlay"
                    )
            if self.anki_rehearsal_mode == "deterministic" and (
                self.anki_rehearsal_egress_pins_json is not None
            ):
                raise ValueError("deterministic rehearsal cannot configure external egress pins")
            if self.anki_rehearsal_mode == "shadow" and not self.anki_rehearsal_egress_pins_json:
                raise ValueError("shadow rehearsal requires pinned provider addresses")
        return self

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
