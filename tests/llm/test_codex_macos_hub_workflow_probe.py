import hashlib
import runpy
import subprocess
from pathlib import Path

import httpx
import pytest
from pptx import Presentation

from oms_hub.tracker_import import parse_tracker


def test_synthetic_workflow_preparation_has_no_native_or_network_calls(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline workflow preparation invoked native/network")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(httpx.Client, "__init__", forbidden)
    script = Path(__file__).parents[2] / "scripts/probe-codex-macos-hub-workflow.py"
    probe = runpy.run_path(str(script))
    artifacts = tmp_path / "synthetic"
    receipt = probe["prepare"](artifacts)
    assert all(
        hashlib.sha256((artifacts / name).read_bytes()).hexdigest() == digest
        for name, digest in receipt.items()
    )
    tracker = parse_tracker(artifacts / "tracker.xlsx")
    assert len(tracker.lectures) == 1 and not tracker.issues
    assert tracker.lectures[0].topic == "Synthetic structures"
    assert len(Presentation(artifacts / "fixture.pptx").slides) == 1
    assert probe["URL"] == "http://127.0.0.1:62170"
    item = {
        "state": "quarantined",
        "sha256": "synthetic-digest",
        "lecture_id": 1,
        "error": "iCloud staging root has not been configured",
    }
    batch = {"kind": "slides", "items": [item]}
    lectures = [{"id": 1, "topic": "Synthetic structures"}]
    assert probe["resume_item"](batch, lectures, "synthetic-digest") == item
    for key, wrong in (
        ("state", "complete"),
        ("sha256", "wrong"),
        ("error", "other failure"),
        ("lecture_id", 2),
    ):
        with pytest.raises(AssertionError):
            probe["resume_item"](
                {"kind": "slides", "items": [item | {key: wrong}]}, lectures, "synthetic-digest"
            )
    transcript = item | {
        "state": "needs_review",
        "error": "Transcript cleaning prompt has not been approved in its current form",
    }
    assert (
        probe["resume_item"](
            {"kind": "transcripts", "items": [transcript]},
            lectures,
            "synthetic-digest",
            transcript=True,
        )
        == transcript
    )
