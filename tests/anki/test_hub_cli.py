import argparse
import json
from types import SimpleNamespace

from oms_hub import cli
from oms_hub.anki.maintenance import LocalIndexRefreshResult


class MemorySecrets:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def test_voyage_set_key_stores_secret_without_printing_it(
    monkeypatch,
    capsys,
) -> None:
    secrets = MemorySecrets()
    monkeypatch.setattr(cli, "KeyringSecretStore", lambda: secrets)
    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        lambda _prompt: "voyage-private",
    )

    assert cli.voyage_set_key(argparse.Namespace()) == 0

    assert secrets.values == {"voyage-api-key": "voyage-private"}
    assert "voyage-private" not in capsys.readouterr().out


def test_anki_index_refresh_prints_a_machine_readable_summary(
    monkeypatch,
    capsys,
) -> None:
    async def refresh(_settings, query: str) -> LocalIndexRefreshResult:
        assert query == 'deck:"AnKing Step Deck"'
        return LocalIndexRefreshResult(
            active_profile="Acceptance Copy",
            companion_generation="local-1",
            semantic_generation="semantic-1",
            note_count=68_000,
            semantic_count=68_000,
            semantic_coverage=1.0,
            duration_ms=1234.5,
            semantic_snapshot_size_bytes=139_264_000,
            peak_memory_bytes=300_000_000,
        )

    monkeypatch.setattr(cli, "_refresh_local_anki_index", refresh)
    monkeypatch.setattr(cli, "Settings", lambda: SimpleNamespace())

    result = cli.anki_index_refresh(
        argparse.Namespace(query='deck:"AnKing Step Deck"')
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["note_count"] == 68_000
    assert payload["semantic_coverage"] == 1.0
    assert payload["semantic_snapshot_size_bytes"] == 139_264_000
    assert payload["peak_memory_bytes"] == 300_000_000


def test_parser_exposes_one_package_anki_setup_commands() -> None:
    parser = cli.build_parser()

    assert parser.parse_args(["voyage-set-key"]).handler is cli.voyage_set_key
    refresh = parser.parse_args(
        ["anki-index-refresh", "--query", "deck:AnKing"]
    )
    assert refresh.handler is cli.anki_index_refresh
    assert refresh.query == "deck:AnKing"
