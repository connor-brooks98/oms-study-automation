import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from oms_anki_agent import __version__
from oms_anki_agent.ankiconnect import AnkiConnectClient
from oms_anki_agent.config import AgentSettings
from oms_anki_agent.hub_client import HubClient
from oms_hub.security.secret_store import KeyringSecretStore

SOURCE_DECK_QUERY = 'deck:"Anking Step Deck"'
TARGET_NOTE_TYPE = "AnKingOverhaul (OMS_II_Extra/JCBrooks)"


class DoctorAnki(Protocol):
    def version(self) -> int: ...

    def find_notes(self, query: str) -> list[int]: ...

    def model_field_names(self, model_name: str) -> list[str]: ...


class DoctorHub(Protocol):
    def health(self) -> dict[str, str]: ...


@dataclass(frozen=True, slots=True)
class DoctorDependencies:
    anki: DoctorAnki
    hub: DoctorHub


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oms-anki-agent")
    parser.add_argument("--version", action="store_true")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("doctor")
    commands.add_parser("run")
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--full", action="store_true", required=True)
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    doctor_dependencies: DoctorDependencies | None = None,
) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    if args.version:
        print(f"oms-anki-agent {__version__}")
        return 0
    if args.command == "doctor":
        dependencies = doctor_dependencies or _doctor_dependencies()
        return _doctor(dependencies)
    if args.command in {"run", "snapshot"}:
        print("Read-only agent service is not installed yet.")
        return 2
    build_parser().print_help()
    return 0


def main() -> None:
    raise SystemExit(run())


def _doctor_dependencies() -> DoctorDependencies:
    # BaseSettings supplies this required field from OMS_ANKI_AGENT_HUB_URL.
    settings = AgentSettings()  # type: ignore[call-arg]
    secrets = KeyringSecretStore()
    return DoctorDependencies(
        anki=AnkiConnectClient(url=settings.ankiconnect_url),
        hub=HubClient(
            hub_url=settings.hub_url,
            agent_id="connor-mac",
            token_key=settings.hub_token_key,
            secrets=secrets,
        ),
    )


def _doctor(dependencies: DoctorDependencies) -> int:
    hub_health = dependencies.hub.health()
    if hub_health.get("status") != "ok":
        print("Hub: unhealthy")
        return 1
    print("Hub: ok")
    dependencies.anki.version()
    notes = dependencies.anki.find_notes(SOURCE_DECK_QUERY)
    if not notes:
        print("Anking Step Deck: missing or empty")
        return 1
    print(f"Anking Step Deck: {len(notes)} notes")
    fields = set(dependencies.anki.model_field_names(TARGET_NOTE_TYPE))
    if not {"Text", "Extra"} <= fields:
        print("Text, Extra: missing")
        return 1
    print("Text, Extra: available")
    return 0
