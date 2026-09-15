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


@pytest.mark.parametrize("needs_preview", [False, True])
def test_blank_evidence_accepts_associated_selected_image(tmp_path, needs_preview):
    source = evidence()
    source["sources"][0]["segments"][0]["text"] = ""
    source["images"][0]["needs_preview"] = needs_preview
    payload = output()
    for question in payload["questions"]:
        question["image"] = deepcopy(payload["questions"][0]["image"])
    assert len(run(Provider(payload), tmp_path, source).questions) == 3


@pytest.mark.parametrize("selected_image", [None, {"source_id": "slides", "asset_key": "a1"}])
def test_blank_evidence_rejects_absent_or_wrong_page_selected_image(tmp_path, selected_image):
    source = evidence()
    source["sources"][0]["segments"][0]["text"] = ""
    source["sources"][0]["segments"][0]["asset_keys"] = []
    source["images"][0]["locator"]["page_number"] = 2
    payload = output()
    for question in payload["questions"]:
        question["image"] = selected_image
    with pytest.raises(SessionError) as error:
        run(Provider(payload), tmp_path, source)
    assert error.value.code == "invalid_output"
    assert "no meaningful text or selected image" in (tmp_path / "invalid.json").read_text()


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


def missing_image_citation():
    source = evidence()
    source["sources"][0]["segments"][0]["asset_keys"] = []
    source["sources"][0]["segments"].append({
        "key": "s2", "text": "Actual image caption", "locator": {"page_number": 2},
    })
    source["images"][0].update(locator={"page_number": 2}, citation_segment_key="s2")
    return source


def test_missing_citation_completion_preserves_original_fields_and_audits_exact_addition(tmp_path):
    from oms_hub.study_generation.gpt_lecture import _digest

    payload = output()
    source = missing_image_citation()
    plan = run(Provider(payload), tmp_path, source)
    expected = deepcopy(payload)
    added = {"source_id": "slides", "segment_key": "s2"}
    expected["questions"][0]["source_segments"].append(added)
    assert plan.model_dump(mode="json") == expected
    record = json.loads((tmp_path / "complete.json").read_text())
    assert record["normalization"] == {
        "version": 1,
        "added_source_segments": [{"question_id": "q0", **added}],
        "before_sha256": _digest(payload),
        "after_sha256": _digest(expected),
    }
    assert json.loads((tmp_path / "raw.txt").read_text()) == payload


def test_completion_can_append_exact_asset_placeholder_without_guessing_same_page_text(tmp_path):
    source = missing_image_citation()
    del source["images"][0]["citation_segment_key"]
    with pytest.raises(SessionError):
        run(Provider(), tmp_path / "no-authoritative-link", source)
    source["sources"][0]["segments"][1].update(text="", asset_keys=["a1"])
    plan = run(Provider(), tmp_path / "explicit-placeholder", source)
    assert plan.questions[0].source_segments[-1].segment_key == "s2"


@pytest.mark.parametrize("change", [
    lambda p: p["questions"][0]["source_segments"].append(
        {"source_id": "slides", "segment_key": "unknown"},
    ),
    lambda p: p["questions"][0].update(image={"source_id": "slides", "asset_key": "unknown"}),
    lambda p: p["questions"][0].update(objective_ids=["unknown"]),
    lambda p: p["questions"][0].update(id="q1"),
])
def test_completion_does_not_repair_invalid_original_references(tmp_path, change):
    payload = output()
    change(payload)
    with pytest.raises(SessionError):
        run(Provider(payload), tmp_path, missing_image_citation())
    assert json.loads((tmp_path / "raw.txt").read_text()) == payload
    assert not (tmp_path / "complete.json").exists()


def retained_plan(
    root, monkeypatch, detail="question image is not associated with its cited source page",
):
    from oms_hub.study_generation import quiz_plan

    def previous_validation(plan, source):
        raise ValueError(detail)

    source = missing_image_citation()
    with monkeypatch.context() as patch:
        patch.setattr(quiz_plan, "_complete_image_citations", previous_validation)
        with pytest.raises(SessionError):
            run(Provider(), root, source)
    return source


def completion_proof():
    return SessionLifecycle("plan-1", "completed", "thread", "turn")


@pytest.mark.parametrize("detail", [
    "question image is not associated with its cited source page",
    "question citation has no meaningful text or selected preview",
])
def test_retained_raw_recovery_requires_no_provider_and_preserves_originals(
    tmp_path, monkeypatch, detail,
):
    from oms_hub.files.atomic import sha256_file

    source = retained_plan(tmp_path, monkeypatch, detail)
    original_invalid = (tmp_path / "invalid.json").read_bytes()
    original_raw = (tmp_path / "raw.txt").read_bytes()
    client = Provider()
    plan = run(client, tmp_path, source, resume=True, completed_lifecycle=completion_proof())
    assert not client.calls
    assert (tmp_path / "invalid.json").read_bytes() == original_invalid
    assert (tmp_path / "raw.txt").read_bytes() == original_raw
    recovery = json.loads((tmp_path / "recovery.json").read_text())
    complete = json.loads((tmp_path / "complete.json").read_text())
    assert recovery["original_invalid_sha256"] == sha256_file(tmp_path / "invalid.json")
    assert recovery["normalized_sha256"] == complete["plan_sha256"]
    assert recovery["normalization"] == complete["normalization"]
    assert recovery["normalization"]["added_source_segments"] == [{
        "question_id": "q0", "source_id": "slides", "segment_key": "s2",
    }]
    assert run(client, tmp_path, source) == plan
    assert not client.calls


@pytest.mark.parametrize("proof", [
    None,
    SessionLifecycle("plan-1", "turn_started", "thread", "turn"),
    SessionLifecycle("wrong-request", "completed", "thread", "turn"),
    SessionLifecycle("plan-1", "completed", "wrong-thread", "turn"),
    SessionLifecycle("plan-1", "completed", "thread", "wrong-turn"),
])
def test_recovery_rejects_missing_or_wrong_completion_proof(tmp_path, monkeypatch, proof):
    source = retained_plan(tmp_path, monkeypatch)
    client = Provider()
    with pytest.raises(SessionError):
        run(client, tmp_path, source, resume=True, completed_lifecycle=proof)
    assert not client.calls
    assert not (tmp_path / "recovery.json").exists()


@pytest.mark.parametrize("tamper", [
    "raw", "provider-descriptor", "dispatch", "truncated", "blank-thread", "invalid-detail",
])
def test_recovery_rejects_changed_or_incomplete_receipts(tmp_path, monkeypatch, tamper):
    source = retained_plan(tmp_path, monkeypatch)
    if tamper == "raw":
        (tmp_path / "raw.txt").write_text("tampered")
    elif tamper == "dispatch":
        (tmp_path / "dispatch.json").write_text('{}')
    elif tamper == "invalid-detail":
        (tmp_path / "invalid.json").write_text(json.dumps({
            "code": "invalid_output", "detail": "question contains an unknown image",
        }))
    else:
        provider_path = tmp_path / "provider.json"
        provider = json.loads(provider_path.read_text())
        if tamper == "provider-descriptor":
            provider["descriptor"]["requested_model"] = "wrong-model"
        elif tamper == "truncated":
            provider["raw_truncated"] = True
        else:
            provider["thread_id"] = ""
        provider_path.write_text(json.dumps(provider))
    client = Provider()
    with pytest.raises(SessionError):
        run(client, tmp_path, source, resume=True, completed_lifecycle=completion_proof())
    assert not client.calls
    assert not (tmp_path / "complete.json").exists()


def test_recovery_requires_explicit_resume_and_exclusive_claim(tmp_path, monkeypatch):
    source = retained_plan(tmp_path, monkeypatch)
    client = Provider()
    with pytest.raises(SessionError):
        run(client, tmp_path, source, completed_lifecycle=completion_proof())
    assert not (tmp_path / "recovery.json").exists()
    (tmp_path / "recovery.json").write_text('{}')
    with pytest.raises(SessionError) as error:
        run(client, tmp_path, source, resume=True, completed_lifecycle=completion_proof())
    assert error.value.code == "interrupted"
    assert not client.calls
