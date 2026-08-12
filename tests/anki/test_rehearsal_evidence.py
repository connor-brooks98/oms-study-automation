from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from oms_hub.anki.rehearsal.evidence import _parse_subcall_ordinal, write_structured_replay_pack


@pytest.mark.parametrize("value, expected", ((0, 0), (17, 17), ("42", 42)))
def test_replay_subcall_ordinal_parser_accepts_bounded_decimal_values(
    value: object, expected: int
) -> None:
    assert _parse_subcall_ordinal(value) == expected


@pytest.mark.parametrize(
    "value",
    (True, False, -1, 1.0, "-1", "+1", "1.0", "one", "", 1 << 63),
)
def test_replay_subcall_ordinal_parser_fails_closed_for_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="subcall ordinal is invalid"):
        _parse_subcall_ordinal(value)


def test_ordinary_redacted_provider_event_ledger_cannot_seed_replay_pack(tmp_path: Path) -> None:
    repository = SimpleNamespace(require_job=lambda _job_id: object())
    with pytest.raises(ValueError, match="redacted"):
        write_structured_replay_pack(repository, UUID(int=1), tmp_path / "structured.json")
    assert not (tmp_path / "structured.json").exists()
