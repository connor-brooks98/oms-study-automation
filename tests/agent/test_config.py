import pytest
from pydantic import ValidationError

from oms_anki_agent.cli import run
from oms_anki_agent.config import AgentSettings


def test_agent_requires_https_hub_url_and_keyring_token_name() -> None:
    config = AgentSettings(
        _env_file=None,
        hub_url="https://study-hub.tailnet-name.ts.net/",
        hub_token_key="anki-agent-token",
    )

    assert config.hub_url == "https://study-hub.tailnet-name.ts.net"
    assert config.hub_token_key == "anki-agent-token"
    assert config.ankiconnect_url == "http://127.0.0.1:8765"


@pytest.mark.parametrize(
    "hub_url",
    [
        "http://study-hub.tailnet-name.ts.net",
        "https://user:password@study-hub.tailnet-name.ts.net",
        "https://study-hub.tailnet-name.ts.net/path",
        "https://study-hub.tailnet-name.ts.net?token=secret",
    ],
)
def test_agent_rejects_unsafe_hub_urls(hub_url: str) -> None:
    with pytest.raises(ValidationError, match="hub_url"):
        AgentSettings(_env_file=None, hub_url=hub_url)


def test_agent_rejects_non_loopback_ankiconnect_override() -> None:
    with pytest.raises(ValidationError, match="ankiconnect_url"):
        AgentSettings(
            _env_file=None,
            hub_url="https://study-hub.tailnet-name.ts.net",
            ankiconnect_url="http://0.0.0.0:8765",
        )


@pytest.mark.parametrize("key", ["", "contains spaces", "token/with/slash"])
def test_agent_rejects_unsafe_keyring_names(key: str) -> None:
    with pytest.raises(ValidationError, match="hub_token_key"):
        AgentSettings(
            _env_file=None,
            hub_url="https://study-hub.tailnet-name.ts.net",
            hub_token_key=key,
        )


def test_agent_cli_reports_version_without_starting_services(capsys) -> None:
    assert run(["--version"]) == 0
    assert capsys.readouterr().out == "oms-anki-agent 0.1.0\n"
