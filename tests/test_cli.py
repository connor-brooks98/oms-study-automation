import argparse
from datetime import UTC, date, datetime
from types import SimpleNamespace

from oms_hub import cli
from oms_hub.outlook.sync import OutlookEvent


class ReadOnlyRepository:
    def list_lectures(self):
        return [
            SimpleNamespace(
                id=41,
                subject="Heme/Lymph",
                exam_number=1,
                lecture_number=4,
                topic="Anemia I",
                lecturer="Jun Wang, MD, PhD",
            )
        ]


class OneEventCalendar:
    def list_events(self, start_utc: datetime, end_utc: datetime):
        return [
            OutlookEvent(
                "event-1",
                "rev-1",
                "4K. Heme/Lymph: Anemia I | Jun Wang, MD, PhD",
                datetime(2026, 7, 1, 13, tzinfo=UTC),
            )
        ]


def test_window_is_bounded_to_requested_utc_days():
    start, end = cli._window(1, date(2026, 7, 1))

    assert start == datetime(2026, 7, 1, tzinfo=UTC)
    assert end == datetime(2026, 7, 2, tzinfo=UTC)


def test_dry_run_prints_proposal_without_requesting_repository_writes(
    monkeypatch,
    capsys,
):
    repository = ReadOnlyRepository()
    monkeypatch.setattr(cli, "_repository", lambda settings: repository)
    monkeypatch.setattr(
        cli,
        "_calendar",
        lambda settings: OneEventCalendar(),
    )

    result = cli.dry_run(argparse.Namespace(date="2026-07-01"))

    assert result == 0
    output = capsys.readouterr().out
    assert "lecture_id=41" in output
    assert "confidence=1.00" in output
    assert "review=False" in output


def test_cli_exposes_phase_one_commands():
    parser = cli.build_parser()

    for command in (
        "import-tracker",
        "outlook-login",
        "sync-outlook",
        "dry-run",
        "serve",
        "canvas-status",
        "canvas-worker-once",
        "canvas-recover",
    ):
        suffix = ["tracker.xlsx"] if command == "import-tracker" else []
        if command == "dry-run":
            suffix = ["--date", "2026-07-01"]
        parsed = parser.parse_args([command, *suffix])
        assert callable(parsed.handler)


def test_cli_exposes_secret_safe_phase_three_commands():
    parser = cli.build_parser()

    for command in (
        "panopto-clear-legacy-credentials",
        "openai-set-key",
        "panopto-init-prompt",
        "panopto-approve-prompt",
        "panopto-status",
        "panopto-scan-once",
        "panopto-worker-once",
        "panopto-recover",
    ):
        parsed = parser.parse_args([command])
        assert callable(parsed.handler)
        assert not hasattr(parsed, "secret")
        assert not hasattr(parsed, "api_key")


def test_openai_key_is_hidden_and_legacy_cleanup_is_explicit(monkeypatch, capsys):
    stored: dict[str, str] = {
        "panopto-client-secret": "stale-secret",
        "panopto-refresh-token": "stale-connection",
        "panopto-oauth-state": "stale-state",
    }

    class MemorySecrets:
        def set(self, key: str, value: str) -> None:
            stored[key] = value

        def delete(self, key: str) -> None:
            stored.pop(key, None)

    monkeypatch.setattr(cli, "KeyringSecretStore", MemorySecrets)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "private-value")

    assert cli.panopto_clear_legacy_credentials(argparse.Namespace()) == 0
    assert cli.openai_set_key(argparse.Namespace()) == 0
    assert stored == {"openai-api-key": "private-value"}
    assert "private-value" not in capsys.readouterr().out
