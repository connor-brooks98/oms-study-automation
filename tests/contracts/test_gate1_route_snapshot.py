from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import pytest
import yaml  # type: ignore[import-untyped]

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.features import FeatureFlag

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_BASE_COMMIT = "86bf2a7de75c5496955f978d97d3bbae075c9fef"
ROUTE_BASELINE_COMMIT = "749d729010aa75bf160f996d39e11edccb883a58"
ROUTE_BASELINE_TREE = "c20af8e2df4991852014ec9f1f66462e5363d71d"
ROUTE_SNAPSHOT_SCHEMA_VERSION = 1
ROUTE_NORMALIZATION_VERSION = "top-level-app-routes-v1"
BASELINE_ROUTE_SNAPSHOT = (
    REPO_ROOT / "artifacts/acceptance/grounded-learning/baseline/route-snapshot-v1.json"
)
MANIFEST = REPO_ROOT / "docs/superpowers/plans/study-hub-parallel-workstream-manifest.yaml"
GATE_RECORD = REPO_ROOT / "artifacts/acceptance/grounded-learning/gate-1.json"
HANDOFF = REPO_ROOT / "docs/implementation/handoffs/0.6.md"
DESIGN = REPO_ROOT / (
    "docs/superpowers/specs/2026-08-20-study-hub-grounded-adaptive-learning-design.md"
)
PLAN = REPO_ROOT / "docs/superpowers/plans/2026-08-20-study-hub-grounded-adaptive-learning.md"
REPO_MAP = REPO_ROOT / "artifacts/implementation/repo-map-v1.json"
SCHEMAS = REPO_ROOT / "schemas"
FIXTURES = REPO_ROOT / "tests/fixtures/grounded_learning"

APPROVED_GROUNDED_FLAGS = {
    "source_trust_v1": False,
    "gemini_file_search_v1": False,
    "ask_studyhub_v1": False,
    "ask_quiz_context_v1": False,
    "board_question_v1": False,
    "adaptive_practice_v1": False,
    "practice_modes_v1": False,
    "error_notebook_v1": False,
    "timed_blocks_v1": False,
    "anki_learning_loop_v1": False,
    "board_runway_v1": False,
    "journal_evidence_v1": False,
}
EXPECTED_FEATURE_FLAG_NAMES = set(APPROVED_GROUNDED_FLAGS) | {
    "legacy_notebooklm_generation"
}
EXPECTED_COMPLETED = ("0.1", "0.2", "0.3", "0.4", "0.5", "0.6")
EXPECTED_READY = (
    "1.1",
    "2.1",
    "3.1",
    "4.1",
    "5.1",
    "5.3",
    "6.3",
    "6.10",
    "6.13",
    "7.1",
    "7.5",
    "8.1",
    "8.2",
    "9.1",
    "9.2",
    "9.5",
)
EXPECTED_WORKSTREAMS = {
    "sol1": ("sol1/source-trust", "1.1"),
    "sol2": ("sol2/gemini-indexing", "2.1"),
    "sol3": ("sol3/ask-backend", "3.1"),
    "sol4": ("sol4/ask-frontend", "4.1"),
    "sol5": ("sol5/board-questions", "5.1", "5.3"),
    "sol6": ("sol6/adaptive-mastery", "6.3"),
    "sol7": ("sol7/anki-runway", "7.1", "7.5"),
    "sol8": ("sol8/journal-evidence", "8.1", "8.2"),
    "sol9": ("sol9/evals-observability-release", "9.1", "9.2", "9.5"),
    "sol10": ("sol10/practice-modes", "6.10", "6.13"),
}
EXPECTED_VERIFICATION_RESULTS = {
    "environment_fingerprint": "pass",
    "route_snapshot": "pass",
    "contracts_providers_features": "pass",
    "settings_generation": "pass",
    "schema_reproducibility": "pass",
    "ruff": "pass",
    "mypy_source": "pass",
    "mypy_phase0_tests": "baseline_exception",
    "javascript": "pass",
    "broad_python": "baseline_exception",
    "native_windows": "baseline_exception",
    "manifest_validation": "pass",
    "gate_json_validation": "pass",
}
EXPECTED_VERIFICATION_COMMANDS = {
    "environment_fingerprint": (
        "python -c 'import importlib.metadata as md, platform, sys; "
        "print(sys.version); print(platform.platform()); print(platform.machine()); "
        "print(platform.processor()); print(md.version(\"PyMuPDF\"))'"
    ),
    "route_snapshot": "python -m pytest tests/contracts/test_gate1_route_snapshot.py -q",
    "contracts_providers_features": (
        "python -m pytest tests/contracts tests/providers tests/features -q"
    ),
    "settings_generation": (
        "python -m pytest tests/anki/test_settings.py tests/v2/test_runtime_settings.py "
        "tests/v2/test_notebook_settings_routes.py tests/v2/test_generation_settings.py -q"
    ),
    "schema_reproducibility": (
        "python -c 'import subprocess,tempfile; output=tempfile.TemporaryDirectory(); "
        "subprocess.run([\"python\",\"scripts/export_grounded_contract_schemas.py\","
        "\"--output-dir\",output.name],check=True); "
        "subprocess.run([\"diff\",\"-ru\",\"schemas\",output.name],check=True)'"
    ),
    "ruff": "ruff check src tests scripts",
    "mypy_source": "mypy src",
    "mypy_phase0_tests": (
        "mypy src/oms_hub src/oms_anki_agent tests/builders/knowledge.py "
        "tests/builders/questions.py tests/contracts/test_fixture_integrity.py "
        "tests/contracts/test_provider_contracts.py tests/contracts/test_schema_exports.py "
        "tests/contracts/test_gate1_route_snapshot.py tests/providers/test_fake_provider.py "
        "tests/features/test_flags.py"
    ),
    "javascript": "node --test \"tests/js/*.test.js\"",
    "broad_python": (
        "python -m pytest -q -m \"not windows_office\" "
        "--deselect tests/document_processing/test_pdf_adapter.py"
    ),
    "native_windows": (
        "gh api 'repos/connor-brooks98/oms-study-automation/actions/runs/32603722284/"
        "jobs?per_page=100' "
        "&& gh run view 32603722284 --repo connor-brooks98/oms-study-automation --log-failed"
    ),
    "manifest_validation": (
        "python -m pytest tests/contracts/test_gate1_route_snapshot.py::"
        "test_manifest_is_exactly_the_gate1_state_transition -q"
    ),
    "gate_json_validation": (
        "python -m pytest tests/contracts/test_gate1_route_snapshot.py::"
        "test_gate_record_matches_manifest_and_all_accepted_hashes -q"
    ),
}
EXPECTED_GATE_TOP_LEVEL_KEYS = {
    "schema_version",
    "gate",
    "state",
    "integration",
    "tasks",
    "contract_tag",
    "checksums",
    "schemas",
    "fixtures",
    "verification",
    "known_baseline_exceptions",
    "workstreams",
}
EXPECTED_INTEGRATION_KEYS = {
    "branch",
    "integration_ref",
    "integration_sha",
    "integration_tree_sha",
    "self_reference_resolution",
}
EXPECTED_VERIFICATION_ENTRY_KEYS = {"name", "command", "result", "evidence"}
SHA256_RE = r"^[0-9a-f]{64}$"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _walk_strings(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_strings(child, (*path, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_strings(child, (*path, str(index)))
    elif isinstance(value, str):
        yield path, value


def _assert_gate_string_policy(gate: dict[str, Any]) -> None:
    worktree_values = {
        entry["worktree"] for entry in gate.get("workstreams", [])
    }
    absolute_path = re.compile(r"(?<![A-Za-z0-9_:/])/[^\s,;)}\]]+")
    env_reference = re.compile(r"\$(?:\{)?[A-Za-z_][A-Za-z0-9_]*")
    env_assignment = re.compile(r"(?:^|\s)[A-Z][A-Z0-9_]*=")
    for path, value in _walk_strings(gate):
        if path[-1] == "worktree":
            assert value in worktree_values
        else:
            assert not absolute_path.search(value), (path, value)
        assert not env_reference.search(value), (path, value)
        assert not env_assignment.search(value), (path, value)
        if "SELF" in value or "SELF_TREE" in value:
            assert path[-1] in {
                "integration_sha",
                "integration_tree_sha",
                "self_reference_resolution",
            }


def _iter_route_objects(routes: Iterable[Any]) -> Iterable[Any]:
    for route in routes:
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        if effective_route_contexts is None:
            yield route
        else:
            yield from effective_route_contexts()


def _normalize_routes(routes: Iterable[Any]) -> list[dict[str, object]]:
    normalized = [
        {
            "name": getattr(route, "name", None) or "",
            "path": route.path,
            "methods": sorted(getattr(route, "methods", None) or ()),
        }
        for route in _iter_route_objects(routes)
    ]
    return sorted(
        normalized,
        key=lambda route: (
            cast(str, route["path"]),
            cast(str, route["name"]),
            tuple(cast(list[str], route["methods"])),
        ),
    )


def _route_diff(
    expected: list[dict[str, object]], actual: list[dict[str, object]]
) -> str:
    expected_by_identity = {(route["path"], route["name"]): route for route in expected}
    actual_by_identity = {(route["path"], route["name"]): route for route in actual}
    added = [
        actual_by_identity[key]
        for key in sorted(actual_by_identity.keys() - expected_by_identity)
    ]
    removed = [
        expected_by_identity[key]
        for key in sorted(expected_by_identity.keys() - actual_by_identity)
    ]
    changed = [
        {"expected": expected_by_identity[key], "actual": actual_by_identity[key]}
        for key in sorted(expected_by_identity.keys() & actual_by_identity)
        if expected_by_identity[key] != actual_by_identity[key]
    ]
    return json.dumps(
        {"added": added, "removed": removed, "changed": changed},
        indent=2,
        sort_keys=True,
    )


def test_feature_flags_off_keep_baseline_routes(tmp_path: Path) -> None:
    assert {flag.value for flag in FeatureFlag} == EXPECTED_FEATURE_FLAG_NAMES
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        allow_local_access=True,
        feature_flags=cast(Any, APPROVED_GROUNDED_FLAGS.copy()),
    )
    assert {
        flag_name: settings.feature_flags.is_enabled(FeatureFlag(flag_name))
        for flag_name in APPROVED_GROUNDED_FLAGS
    } == APPROVED_GROUNDED_FLAGS
    assert settings.feature_flags.is_enabled(FeatureFlag.LEGACY_NOTEBOOKLM_GENERATION)

    app = create_app(settings)
    try:
        actual = _normalize_routes(app.routes)
    finally:
        app.state.database.close()

    snapshot = json.loads(BASELINE_ROUTE_SNAPSHOT.read_text(encoding="utf-8"))
    assert isinstance(snapshot, dict)
    assert set(snapshot) == {
        "schema_version",
        "source_commit",
        "source_tree",
        "normalization_version",
        "routes",
    }
    assert snapshot["schema_version"] == ROUTE_SNAPSHOT_SCHEMA_VERSION
    assert snapshot["source_commit"] == ROUTE_BASELINE_COMMIT
    assert snapshot["source_tree"] == ROUTE_BASELINE_TREE
    assert snapshot["normalization_version"] == ROUTE_NORMALIZATION_VERSION
    expected = snapshot["routes"]
    assert actual == expected, _route_diff(expected, actual)


def _load_manifest() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(MANIFEST.read_text(encoding="utf-8")))


def _load_base_manifest() -> dict[str, Any]:
    raw = subprocess.check_output(
        [
            "git",
            "show",
            f"{MANIFEST_BASE_COMMIT}:docs/superpowers/plans/study-hub-parallel-workstream-manifest.yaml",
        ],
        cwd=REPO_ROOT,
        text=True,
    )
    return cast(dict[str, Any], yaml.safe_load(raw))


def _manifest_after_authorized_transition() -> dict[str, Any]:
    expected = _load_base_manifest()
    for task in expected["tasks"]:
        task["initial_state"] = (
            "complete"
            if task["id"] in EXPECTED_COMPLETED
            else "ready"
            if task["id"] in EXPECTED_READY
            else "blocked"
        )
    expected["gates"]["gate_1_shared_contracts"]["state"] = "open"
    return expected


def test_manifest_is_exactly_the_gate1_state_transition() -> None:
    manifest = _load_manifest()
    assert manifest == _manifest_after_authorized_transition()

    tasks = manifest["tasks"]
    task_by_id = {task["id"]: task for task in tasks}
    assert len(task_by_id) == len(tasks) == 90
    assert {task["initial_state"] for task in tasks} <= {"complete", "ready", "blocked"}

    dependencies = {
        task_id: set(task["depends_on"])
        for task_id, task in task_by_id.items()
    }
    assert all(
        dependency in task_by_id
        for values in dependencies.values()
        for dependency in values
    )

    resolved: set[str] = set()
    while unresolved := {
        task_id
        for task_id, values in dependencies.items()
        if task_id not in resolved and not values - resolved
    }:
        resolved.update(unresolved)
    assert resolved == set(task_by_id)

    complete = {task["id"] for task in tasks if task["initial_state"] == "complete"}
    ready = {task["id"] for task in tasks if task["initial_state"] == "ready"}
    blocked = {task["id"] for task in tasks if task["initial_state"] == "blocked"}
    assert complete == set(EXPECTED_COMPLETED)
    assert ready == set(EXPECTED_READY)
    assert len(blocked) == 68
    assert all(dependencies[task_id] <= complete for task_id in ready)
    assert [task_id for task_id in blocked if dependencies[task_id] <= complete] == []

    gates = manifest["gates"]
    assert gates["gate_1_shared_contracts"]["state"] == "open"
    assert all(
        gate["state"] == "blocked"
        for gate_id, gate in gates.items()
        if gate_id != "gate_1_shared_contracts"
    )


@pytest.mark.parametrize("embedded_path", ("python /tmp/tool.py", "note=/Users/connor/x"))
def test_gate_string_policy_rejects_embedded_absolute_paths(embedded_path: str) -> None:
    with pytest.raises(AssertionError):
        _assert_gate_string_policy({"verification": [{"command": embedded_path}]})
    _assert_gate_string_policy({"verification": [{"command": "https://example.com/a"}]})


def test_gate_schema_command_executes_twice_from_repo_root() -> None:
    gate = cast(dict[str, Any], json.loads(GATE_RECORD.read_text(encoding="utf-8")))
    command = next(
        entry["command"]
        for entry in gate["verification"]
        if entry["name"] == "schema_reproducibility"
    )
    assert command == EXPECTED_VERIFICATION_COMMANDS["schema_reproducibility"]
    for _ in range(2):
        subprocess.run(command, shell=True, cwd=REPO_ROOT, check=True)


def test_handoff_records_current_gate_checksum() -> None:
    text = HANDOFF.read_text(encoding="utf-8")
    gate_section = text.split("## Gate record", 1)[1].split("## Checksums", 1)[0]
    match = re.search(r"(?m)^- SHA-256: `([0-9a-f]{64})`$", gate_section)
    assert match is not None
    assert match.group(1) == _sha256(GATE_RECORD)


def test_gate_record_matches_manifest_and_all_accepted_hashes() -> None:
    gate = cast(dict[str, Any], json.loads(GATE_RECORD.read_text(encoding="utf-8")))
    assert set(gate) == EXPECTED_GATE_TOP_LEVEL_KEYS
    assert gate["schema_version"] == 1
    assert gate["gate"] == "gate_1_shared_contracts"
    assert gate["state"] == "open"
    assert set(gate["integration"]) == EXPECTED_INTEGRATION_KEYS
    assert gate["integration"] == {
        "branch": "integration/studyhub-grounded-learning-v1",
        "integration_ref": "integration/studyhub-grounded-learning-v1",
        "integration_sha": "SELF",
        "integration_tree_sha": "SELF_TREE",
        "self_reference_resolution": (
            "Resolve SELF and SELF_TREE from the commit containing this record."
        ),
    }
    assert gate["tasks"] == {
        "completed": list(EXPECTED_COMPLETED),
        "ready": list(EXPECTED_READY),
    }
    assert gate["contract_tag"] == {
        "name": "studyhub-grounded-contracts-v1",
        "annotated_tag_object": "a2636bbeb84d2143685c3555e9a3f74ccb8380d0",
        "peeled_target": "60a5f3ec873f982bca14d3507d719eb9927a8f1a",
    }

    checksum_paths = {
        "repo_map": REPO_MAP,
        "design": DESIGN,
        "plan": PLAN,
        "manifest": MANIFEST,
        "route_snapshot": BASELINE_ROUTE_SNAPSHOT,
    }
    assert gate["checksums"] == {
        name: _sha256(path) for name, path in checksum_paths.items()
    }
    assert all(
        isinstance(value, str) and re.fullmatch(SHA256_RE, value)
        for value in gate["checksums"].values()
    )

    schema_paths = {
        f"{name}-v1.json": SCHEMAS / f"{name}-v1.json"
        for name in ("knowledge", "ask", "question", "mastery", "practice", "journal")
    }
    assert gate["schemas"] == {name: _sha256(path) for name, path in schema_paths.items()}
    fixture_paths = {
        "lecture-13-normalized.md": FIXTURES / "course/lecture-13-normalized.md",
        "lecture-13-pages.json": FIXTURES / "course/lecture-13-pages.json",
        "article-1-normalized.md": FIXTURES / "literature/article-1-normalized.md",
        "README.md": FIXTURES / "README.md",
    }
    assert gate["fixtures"] == {name: _sha256(path) for name, path in fixture_paths.items()}

    verification = gate["verification"]
    assert all(set(entry) == EXPECTED_VERIFICATION_ENTRY_KEYS for entry in verification)
    assert [entry["name"] for entry in verification] == list(EXPECTED_VERIFICATION_RESULTS)
    assert {
        entry["name"]: entry["command"] for entry in verification
    } == EXPECTED_VERIFICATION_COMMANDS
    assert {
        entry["name"]: entry["result"] for entry in verification
    } == EXPECTED_VERIFICATION_RESULTS
    assert all(entry["evidence"] == "docs/implementation/handoffs/0.6.md" for entry in verification)
    assert gate["known_baseline_exceptions"] == [
        {
            "id": "windows_native_acceptance",
            "status": "outstanding",
            "detail": "No native Windows pass is claimed.",
        },
        {
            "id": "pymupdf_macos_cpython312",
            "status": "inherited",
            "detail": (
                "CPython 3.12.13 and PyMuPDF 1.28.2 reproduce signal 11 "
                "in the recorded baseline path."
            ),
        },
        {
            "id": "phase0_test_typing",
            "status": "inherited",
            "detail": (
                "The frozen tests/features/test_flags.py has 13 strict-mypy errors; "
                "Task 0.6 does not edit or suppress that file."
            ),
        },
    ]

    workstreams = gate["workstreams"]
    assert [entry["id"] for entry in workstreams] == [f"sol{number}" for number in range(1, 11)]
    for entry in workstreams:
        workstream_id = entry["id"]
        branch_and_tasks = EXPECTED_WORKSTREAMS[workstream_id]
        assert entry == {
            "id": workstream_id,
            "branch": branch_and_tasks[0],
            "worktree": str(
                Path("/Users/connor/Developer/worktrees/oms-study-automation-grounded-learning")
                / workstream_id
            ),
            "ready_tasks": list(branch_and_tasks[1:]),
        }

    _assert_gate_string_policy(gate)
