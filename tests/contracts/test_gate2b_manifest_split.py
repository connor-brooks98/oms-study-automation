from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "docs/superpowers/plans/study-hub-parallel-workstream-manifest.yaml"


def _load_manifest() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(MANIFEST.read_text()))


def test_gate_2b_is_delivered_only_by_the_final_evidence_integration_task() -> None:
    manifest = _load_manifest()
    tasks = cast(list[dict[str, Any]], manifest["tasks"])
    task_by_id = {cast(str, task["id"]): task for task in tasks}
    expected = {
        "2.8a": (
            "Public multimodal Gemini contract matrix",
            "sol2",
            "sol2/gemini-indexing",
            ["2.7"],
        ),
        "2.8b": (
            "Tracked Windows private-shadow composition",
            "sol0",
            "sol0/contracts-and-integration",
            ["2.8a"],
        ),
        "2.8c": ("Bounded private Lecture 13 acceptance", "sol2", "sol2/gemini-indexing", ["2.8b"]),
        "2.8d": (
            "Gate 2B evidence acceptance and integration",
            "sol0",
            "sol0/contracts-and-integration",
            ["2.8c"],
        ),
    }

    assert "2.8" not in task_by_id
    for task_id, (title, workstream, branch, dependencies) in expected.items():
        task = task_by_id[task_id]
        assert task["title"] == title
        assert task["workstream"] == workstream
        assert task["branch"] == branch
        assert task["depends_on"] == dependencies
        assert task["wave"] == 3
        assert task["initial_state"] == "blocked"
        assert task["handoff"] == f"docs/implementation/handoffs/{task_id}.md"
        assert task["reviewers"] == ["terra_spec", "terra_quality"]

    assert manifest["gates"]["gate_2b_gemini_provider"]["task"] == "2.8d"
    assert task_by_id["2.8d"]["delivers_gate"] == "gate_2b_gemini_provider"
    assert all("delivers_gate" not in task_by_id[task_id] for task_id in ("2.8a", "2.8b", "2.8c"))
    assert all("2.8" not in cast(list[str], task["depends_on"]) for task in tasks)
    assert [task["id"] for task in tasks if "2.8d" in task["depends_on"]] == [
        "3.4",
        "5.5",
        "8.4",
        "9.3",
        "9.7",
    ]

    workstreams = {
        cast(str, workstream["id"]): cast(list[str], workstream["tasks"])
        for workstream in cast(list[dict[str, Any]], manifest["workstreams"])
    }
    assert "2.8" not in workstreams["sol2"]
    assert all(task_id in workstreams["sol2"] for task_id in ("2.8a", "2.8c"))
    assert all(task_id in workstreams["sol0"] for task_id in ("2.8b", "2.8d"))
