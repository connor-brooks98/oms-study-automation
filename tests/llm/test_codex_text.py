import json

import pytest

from oms_hub.llm.codex_session import SessionError, SessionRequest
from oms_hub.llm.codex_text import generate_bound_text
from tests.v2.test_codex_transcript_cleaner import SessionFixture


def test_failed_dispatch_callback_retains_claim_and_cannot_retry(tmp_path):
    client = SessionFixture()
    journal = tmp_path / "attempt.json"
    request = SessionRequest("bound", "fixture", "instructions", "source")

    def reject(event):
        assert json.loads(journal.read_text())["events"][-1]["phase"] == "dispatching"
        raise SessionError("interrupted")

    with pytest.raises(SessionError):
        generate_bound_text(client, request, journal, {"id": "bound"}, on_lifecycle=reject)
    assert journal.exists()
    assert not list(tmp_path.glob("*.blocked-*.json"))
    with pytest.raises(SessionError):
        generate_bound_text(client, request, journal, {"id": "bound"})
    assert client.calls == 1


def test_invalid_output_is_retained_before_validation_and_never_replayed(tmp_path):
    client = SessionFixture(output='{"bad": 1}')
    journal = tmp_path / "attempt.json"
    request = SessionRequest("bound", "fixture", "instructions", "source")
    with pytest.raises(SessionError, match="invalid output"):
        generate_bound_text(client, request, journal, {"id": "bound"})
    assert json.loads(journal.read_text())["raw_response"] == '{"bad": 1}'
    with pytest.raises(SessionError):
        generate_bound_text(client, request, journal, {"id": "bound"})
    assert client.calls == 1
