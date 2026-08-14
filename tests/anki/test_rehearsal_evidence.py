from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from oms_hub.anki.rehearsal.evidence import _parse_subcall_ordinal, write_structured_replay_pack
from oms_hub.anki.rehearsal.process import (
    ProcessObservation,
    ProcessRehearsal,
    RehearsalRequest,
)


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


def test_expected_replay_miss_evidence_is_partial_redacted_and_hash_manifested(
    tmp_path: Path,
) -> None:
    request = RehearsalRequest(
        capsule=tmp_path / "capsule",
        overlay=tmp_path / "overlay",
        mode="deterministic",
        port=8788,
        evidence_zip=tmp_path / "evidence.zip",
        failed_job_id=UUID(int=1),
        expected_manifest_sha256="0" * 64,
        implementation_repository=tmp_path / "implementation",
        expected_implementation_commit="a" * 40,
        expected_implementation_tree="b" * 40,
        trusted_python=Path(sys.executable),
        run_goal="first_replay_miss",
        restart_after_durable_boundary=False,
    )
    harness = ProcessRehearsal(request)
    harness._source_attestation = {"pid": 7, "source_tree_sha256": "c" * 64}
    stdout, stderr = tmp_path / "stdout.log", tmp_path / "stderr.log"
    stdout.write_text("not packaged", encoding="utf-8")
    stderr.write_text("not packaged", encoding="utf-8")
    harness._children = [
        SimpleNamespace(
            observation=ProcessObservation(7, 7, "start", "end", 0, ("python", "serve")),
            stdout_path=stdout,
            stderr_path=stderr,
        )
    ]
    row = {
        "id": 1,
        "event": "begun",
        "response_text": None,
        "response_sha256": None,
    }
    repository = SimpleNamespace(list_provider_attempt_events=lambda _job_id: [row])
    client = SimpleNamespace(
        transcript=[
            {
                "method": "POST",
                "path": "/api/anki/jobs",
                "status": 201,
                "request_body": {"instruction_text": "prompt plaintext must not escape"},
                "response_body": {"id": "job"},
            }
        ]
    )
    harness._write_expected_replay_miss_evidence(
        SimpleNamespace(model_dump=lambda **_kwargs: {"identity": "capsule"}),
        SimpleNamespace(
            root=tmp_path / "overlay",
            database_path=tmp_path / "overlay/db.sqlite",
            path_audit=[],
        ),
        client,
        repository,
        UUID(int=2),
        {"state": "failed", "error": "missing structured replay response " + "1" * 64},
        {"kind": "structured_empty_pack", "key": "1" * 64},
        {"schema_version": 1, "run_nonce": "nonce", "records": []},
        {
            "schema_version": 1,
            "run_nonce": "nonce",
            "mode": "deterministic",
            "records": [],
        },
    )
    with zipfile.ZipFile(request.evidence_zip) as archive:
        names = set(archive.namelist())
        assert {
            "outcome.json",
            "implementation.json",
            "overlay.json",
            "sha256-manifest.json",
        } <= names
        outcome = json.loads(archive.read("outcome.json"))
        assert outcome["result"] == "EXPECTED_REPLAY_MISS"
        assert outcome["ready_for_review"] is False
        transcript = archive.read("http-transcript.json")
        assert b"prompt plaintext" not in transcript
        assert b"request_sha256" in transcript and b"response_sha256" in transcript
        manifest = json.loads(archive.read("sha256-manifest.json"))
        assert set(manifest) == names - {"sha256-manifest.json"}
