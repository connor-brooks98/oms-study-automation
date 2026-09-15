import json
from copy import deepcopy

import pytest

from oms_hub.llm.codex_session import SessionError, SessionLifecycle, SessionResult
from oms_hub.study_generation.quiz_plan import _question_count_bounds, plan_lecture_quiz


def evidence():
    return {
        "objectives": [{"id": "o1", "text": "Identify the tissue"}],
        "sources": [{"source_id": "slides", "segments": [{
            "key": "s1", "text": "A tissue with stratified epithelial cells.",
            "locator": {"page_number": 1}, "asset_keys": ["a1"],
        }]}],
        "images": [{
            "source_id": "slides", "asset_key": "a1", "locator": {"page_number": 1},
        }],
    }


def output():
    return {"title": "Tissue quiz", "questions": [{
        "id": f"q{i}", "focus": f"Recognize tissue feature {i}", "objective_ids": ["o1"],
        "source_segments": [{"source_id": "slides", "segment_key": "s1"}],
        "image": {"source_id": "slides", "asset_key": "a1"} if i == 0 else None,
    } for i in range(3)]}


class Provider:
    def __init__(self, payload=None, *, fail=None):
        self.payload = output() if payload is None else payload
        self.fail = fail
        self.calls = []

    def generate(self, request, *, cancelled, on_lifecycle):
        self.calls.append(request)
        assert not request.image_paths
        assert not request.image_sha256
        if self.fail == "preflight":
            raise SessionError("context_limit")
        on_lifecycle(SessionLifecycle(request.request_id, "dispatching"))
        if self.fail == "started":
            raise SessionError("interrupted")
        on_lifecycle(SessionLifecycle(request.request_id, "completed", "thread", "turn"))
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return SessionResult("thread", "turn", text)


def run(client, root, source=None, **kwargs):
    return plan_lecture_quiz(
        client, "plan-1", "fixture-model", evidence() if source is None else source,
        root=root, cancelled=lambda: False, on_lifecycle=lambda event: None, **kwargs,
    )


def test_text_only_plan_is_cached_and_bound_to_evidence_model_prompt_schema(tmp_path):
    client = Provider()
    plan = run(client, tmp_path)
    assert len(plan.questions) == 3
    assert json.loads(client.calls[0].source_text) == evidence()
    assert run(client, tmp_path) == plan
    assert run(client, tmp_path, resume=True) == plan
    assert len(client.calls) == 1
    changed = evidence()
    changed["objectives"][0]["text"] = "Changed objective"
    with pytest.raises(SessionError):
        run(client, tmp_path, changed, resume=True)
    with pytest.raises(SessionError):
        plan_lecture_quiz(client, "plan-1", "another-model", evidence(), root=tmp_path,
                          cancelled=lambda: False, on_lifecycle=lambda event: None, resume=True)
    assert len(client.calls) == 1
    descriptor = json.loads((tmp_path / "plan.json").read_text())
    assert {"evidence_sha256", "schema_sha256", "prompt_sha256"} <= descriptor.keys()


@pytest.mark.parametrize("change", [
    lambda p: p["questions"][0].update(objective_ids=["unknown"]),
    lambda p: p["questions"][0].update(objective_ids=["o1", "o1"]),
    lambda p: p["questions"][0].update(id="q1"),
    lambda p: p["questions"][0].update(image={"source_id": "slides", "asset_key": "bad"}),
    lambda p: p["questions"][0].update(source_segments=[{"source_id": "bad", "segment_key": "s1"}]),
    lambda p: p["questions"][0].update(source_segments=[
        {"source_id": "slides", "segment_key": "bad"},
    ]),
    lambda p: p["questions"][0].update(correct_index=1),
    lambda p: p["questions"][0].update(focus="  "),
    lambda p: p.update(questions=p["questions"][:2]),
])
def test_invalid_plan_retains_raw_and_cannot_replay(tmp_path, change):
    payload = output()
    change(payload)
    client = Provider(payload)
    with pytest.raises(SessionError) as error:
        run(client, tmp_path)
    assert error.value.code == "invalid_output"
    assert json.loads((tmp_path / "raw.txt").read_text()) == payload
    assert (tmp_path / "invalid.json").exists()
    with pytest.raises(SessionError):
        run(client, tmp_path, resume=True)
    assert len(client.calls) == 1


@pytest.mark.parametrize("raw", ["not json", '{"title": "x", "questions": []}', '[]'])
def test_malformed_response_is_retained(tmp_path, raw):
    with pytest.raises(SessionError):
        run(Provider(raw), tmp_path)
    assert (tmp_path / "raw.txt").read_text() == raw


def test_all_required_objectives_must_be_covered(tmp_path):
    source = evidence()
    source["objectives"].append({"id": "o2", "text": "Another topic"})
    with pytest.raises(SessionError):
        run(Provider(), tmp_path, source)
    assert "uncovered objectives o2" in (tmp_path / "invalid.json").read_text()


def test_selected_image_requires_same_page_source_citation(tmp_path):
    source = evidence()
    source["images"][0]["locator"]["page_number"] = 2
    source["sources"][0]["segments"][0]["asset_keys"] = []
    with pytest.raises(SessionError):
        run(Provider(), tmp_path, source)
    assert "associated" in (tmp_path / "invalid.json").read_text()


def test_blank_evidence_requires_selected_needs_preview_image(tmp_path):
    source = evidence()
    source["sources"][0]["segments"][0]["text"] = ""
    payload = output()
    for question in payload["questions"]:
        question["image"] = deepcopy(payload["questions"][0]["image"])
    with pytest.raises(SessionError):
        run(Provider(payload), tmp_path / "no-preview", source)
    source["images"][0]["needs_preview"] = True
    assert len(run(Provider(payload), tmp_path / "preview", source).questions) == 3


def test_started_request_and_failed_observer_never_replay(tmp_path):
    client = Provider(fail="started")
    for resume in (False, True):
        with pytest.raises(SessionError):
            run(client, tmp_path / "started", resume=resume)
    assert len(client.calls) == 1

    client = Provider()

    def fail(event):
        raise SessionError("interrupted")

    with pytest.raises(SessionError):
        plan_lecture_quiz(client, "plan-1", "fixture-model", evidence(), root=tmp_path / "observer",
                          cancelled=lambda: False, on_lifecycle=fail)
    with pytest.raises(SessionError):
        run(client, tmp_path / "observer", resume=True)
    assert len(client.calls) == 1
    assert not list((tmp_path / "observer").glob("preflight-*.json"))


def test_zero_event_preflight_requires_explicit_resume_and_preserves_diagnostic(tmp_path):
    client = Provider(fail="preflight")
    with pytest.raises(SessionError):
        run(client, tmp_path)
    assert not (tmp_path / "dispatch.json").exists()
    assert len(list(tmp_path.glob("preflight-*-context_limit.json"))) == 1
    client.fail = None
    with pytest.raises(SessionError):
        run(client, tmp_path)
    assert len(client.calls) == 1
    assert run(client, tmp_path, resume=True)
    assert len(client.calls) == 2


def test_exclusive_dispatch_prevents_nested_concurrent_call(tmp_path):
    client = Provider()
    nested = Provider()

    def attempt(event):
        with pytest.raises(SessionError) as error:
            run(nested, tmp_path, resume=True)
        assert error.value.code == "interrupted"

    plan_lecture_quiz(client, "plan-1", "fixture-model", evidence(), root=tmp_path,
                      cancelled=lambda: False, on_lifecycle=attempt)
    assert len(client.calls) == 1
    assert not nested.calls


def test_text_limit_and_more_than_twenty_inventory_images(tmp_path):
    source = evidence()
    source["sources"][0]["segments"][0]["text"] = "x" * 100_000
    client = Provider()
    with pytest.raises(SessionError) as error:
        run(client, tmp_path / "too-large", source)
    assert error.value.code == "context_limit"
    assert not client.calls
    source = evidence()
    source["images"] += [dict(source["images"][0], asset_key=f"extra-{i}") for i in range(21)]
    assert run(client, tmp_path / "inventory", source)


def test_cache_tampering_fails_without_replay(tmp_path):
    client = Provider()
    run(client, tmp_path)
    (tmp_path / "raw.txt").write_text("tampered")
    with pytest.raises(SessionError):
        run(client, tmp_path, resume=True)
    assert len(client.calls) == 1


def test_plan_can_select_more_than_twenty_images_for_later_batching(tmp_path):
    source = evidence()
    payload = output()
    source["images"] = []
    source["sources"][0]["segments"] = []
    payload["questions"] = []
    for index in range(21):
        source["images"].append({
            "source_id": "slides", "asset_key": f"a{index}",
            "locator": {"page_number": index + 1},
        })
        source["sources"][0]["segments"].append({
            "key": f"s{index}", "text": f"Tissue feature {index}",
            "locator": {"page_number": index + 1},
        })
        payload["questions"].append({
            "id": f"q{index}", "focus": f"Identify tissue feature {index}",
            "objective_ids": ["o1"],
            "source_segments": [{"source_id": "slides", "segment_key": f"s{index}"}],
            "image": {"source_id": "slides", "asset_key": f"a{index}"},
        })
    client = Provider(payload)
    assert len(run(client, tmp_path, source).questions) == 21
    assert not client.calls[0].image_paths


def test_missing_completion_retains_raw_and_blocks_resume(tmp_path):
    class IncompleteProvider(Provider):
        def generate(self, request, *, cancelled, on_lifecycle):
            self.calls.append(request)
            on_lifecycle(SessionLifecycle(request.request_id, "turn_started", "thread", "turn"))
            return SessionResult("thread", "turn", json.dumps(output()))

    client = IncompleteProvider()
    with pytest.raises(SessionError):
        run(client, tmp_path)
    assert json.loads((tmp_path / "raw.txt").read_text()) == output()
    with pytest.raises(SessionError):
        run(client, tmp_path, resume=True)
    assert len(client.calls) == 1


@pytest.mark.parametrize("instructions,bounds", [
    ("Generate exactly15 questions.", (15, 15)),
    ("Generate 15 questions.", (15, 15)),
    ("A 15-question quiz.", (15, 15)),
    ("Generate 12-15 questions.", (12, 15)),
    ("Generate 12–15 questions.", (12, 15)),
    ("Generate 12 to 15 questions.", (12, 15)),
    ("12-15 questions; exactly 15 questions.", (15, 15)),
    ("Generate at least 15 questions.", (15, 500)),
    ("Generate at most 15 questions.", (3, 15)),
    ("Generate up to 15 questions.", (3, 15)),
    ("At least 15 questions, at most 20 questions.", (15, 20)),
    ("Generate 15 questions per objective.", None),
    ("Generate 15 questions per slide.", None),
    ("Generate 15 questions for each topic.", None),
    ("Generate 15 questions/objective.", None),
    ("Generate 1 question per objective, up to 20 questions total.", (3, 20)),
    ("Use slides 12-15, with 5 choices and fifteen questions.", None),
])
def test_numeric_question_count_recognition(instructions, bounds):
    assert _question_count_bounds(instructions) == bounds


@pytest.mark.parametrize("instructions", [
    "15 questions and 20 questions", "15-12 questions", "2 questions", "501 questions",
    "0 questions", "-15 questions", "12-501 questions",
    "At least 15 questions and at most 12 questions",
])
def test_invalid_requested_counts_reject_before_provider(tmp_path, instructions):
    source = evidence() | {"quiz_instructions": instructions}
    client = Provider()
    with pytest.raises(SessionError) as error:
        run(client, tmp_path, source)
    assert error.value.code == "invalid_output"
    assert not client.calls


@pytest.mark.parametrize("instructions,accepted", [
    ("Generate exactly 15 questions.", False),
    ("Generate a 15-question quiz.", False),
    ("Generate 4-15 questions.", False),
    ("Generate 3-15 questions.", True),
    ("Generate exactly3questions.", True),
    ("Use slides 12-15 with 5 choices per question.", True),
    ("Generate up to 15 questions.", True),
    ("Generate at most 15 questions.", True),
    ("Generate at least 3 questions.", True),
    ("Generate at least 15 questions.", False),
    ("Generate 15 questions per objective.", True),
])
def test_plan_enforces_count_only_from_user_instructions(tmp_path, instructions, accepted):
    source = evidence() | {"quiz_instructions": instructions}
    source["sources"][0]["segments"][0]["text"] += " Generate 100 questions."
    client = Provider()
    if accepted:
        assert len(run(client, tmp_path, source).questions) == 3
    else:
        with pytest.raises(SessionError) as error:
            run(client, tmp_path, source)
        assert error.value.code == "invalid_output"
        assert "does not match requested" in (tmp_path / "invalid.json").read_text()
    assert json.loads(client.calls[0].source_text)["quiz_instructions"] == instructions


def test_cached_plan_is_revalidated_against_count(tmp_path):
    from oms_hub.study_generation.gpt_lecture import _digest

    source = evidence() | {"quiz_instructions": "Generate 3 questions."}
    client = Provider()
    run(client, tmp_path, source)
    complete_path = tmp_path / "complete.json"
    record = json.loads(complete_path.read_text())
    question = deepcopy(record["plan"]["questions"][0])
    question["id"] = "q-extra"
    record["plan"]["questions"].append(question)
    record["plan_sha256"] = _digest(record["plan"])
    complete_path.write_text(json.dumps(record))
    with pytest.raises(SessionError) as error:
        run(client, tmp_path, source, resume=True)
    assert error.value.code == "invalid_output"
    assert len(client.calls) == 1
