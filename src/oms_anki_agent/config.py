import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OMS_ANKI_AGENT_",
        env_file=".env",
        extra="ignore",
    )

    hub_url: str
    agent_id: str = "connor-mac"
    hub_token_key: str = "anki-agent-token"
    ankiconnect_url: Literal["http://127.0.0.1:8765"] = "http://127.0.0.1:8765"
    poll_seconds: float = Field(default=5.0, ge=0.5, le=60.0)
    request_timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)

    @field_validator("hub_url")
    @classmethod
    def validate_hub_url(cls, value: str) -> str:
        candidate = value.strip()
        parsed = urlsplit(candidate)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("hub_url must be an HTTPS origin without credentials or a path")
        hostname = parsed.hostname.casefold().rstrip(".")
        if not re.fullmatch(
            r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
            r"[a-z]{2,63}",
            hostname,
        ):
            raise ValueError("hub_url must contain a valid hostname")
        return f"https://{hostname}"

    @field_validator("hub_token_key")
    @classmethod
    def validate_hub_token_key(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", normalized):
            raise ValueError("hub_token_key contains unsupported characters")
        return normalized

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", normalized):
            raise ValueError("agent_id contains unsupported characters")
        return normalized
