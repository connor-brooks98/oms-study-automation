from oms_anki_agent.cli import DoctorDependencies, run


class HealthyAnki:
    def version(self) -> int:
        return 6

    def find_notes(self, query: str) -> list[int]:
        assert query == 'deck:"Anking Step Deck"'
        return [11]

    def model_field_names(self, model_name: str) -> list[str]:
        assert model_name == "AnKingOverhaul (OMS_II_Extra/JCBrooks)"
        return ["Text", "Extra", "Missed Questions"]


class HealthyHub:
    def health(self) -> dict[str, str]:
        return {"status": "ok"}


def test_cli_exposes_read_only_commands() -> None:
    assert run(["--help"]) == 0


def test_doctor_checks_hub_source_deck_and_runtime_fields(capsys) -> None:
    dependencies = DoctorDependencies(anki=HealthyAnki(), hub=HealthyHub())

    assert run(["doctor"], doctor_dependencies=dependencies) == 0
    output = capsys.readouterr().out
    assert "Hub: ok" in output
    assert "Anking Step Deck: 1 notes" in output
    assert "Text, Extra: available" in output


def test_snapshot_command_requires_full_flag() -> None:
    assert run(["snapshot"]) == 2
