import json
from copy import deepcopy
from dataclasses import replace

from oms_hub.document_processing.domain import DocumentLocator
from oms_hub.study_generation import quiz_selection
from oms_hub.study_generation.gpt_lecture import (
    MAX_BATCH_IMAGES,
    MAX_SOURCE_CHARACTERS,
    _canonical,
    source_manifest,
)
from oms_hub.study_generation.quiz_evidence import compact_evidence
from oms_hub.study_generation.quiz_plan import QuizPlan, validate_quiz_plan
from tests.study_generation.test_gpt_lecture import _inputs


def test_selected_images_split_and_rebalance_short_tail_without_changing_question_order(
    tmp_path, monkeypatch,
):
    inputs = _inputs(tmp_path)
    slides, transcript = inputs.documents
    assets = tuple(replace(
        slides.assets[0], key=f"figure-{index:02}",
        locator=DocumentLocator(f"slide {index + 1}", slide_number=index + 1),
    ) for index in range(1, 23))
    segments = tuple(replace(
        slides.segments[0], key=f"evidence-{index:02}", locator=asset.locator,
        text=f"Independent native evidence for figure {index}. " * 4,
        asset_keys=(asset.key,),
    ) for index, asset in enumerate(assets, start=1))
    inputs = replace(
        inputs, documents=(replace(slides, assets=assets, segments=segments), transcript),
        instructions="22 questions",
    )
    plan = QuizPlan.model_validate({
        "title": "Twenty-two distinct image questions",
        "questions": [{
            "id": f"question-{index:02}",
            "focus": f"Interpret the evidence on slide {index + 1}.",
            "objective_ids": [key for key, _ in inputs.objectives],
            "source_segments": [{"source_id": slides.source_id, "segment_key": segment.key}],
            "image": {"source_id": slides.source_id, "asset_key": asset.key},
        } for index, (segment, asset) in enumerate(zip(segments, assets, strict=True), start=1)],
    })
    planning_calls = []

    def planner(client, request_id, model, evidence, **kwargs):
        planning_calls.append(request_id)
        assert len(evidence["images"]) == 22
        assert not any(image.get("needs_preview") for image in evidence["images"])
        validate_quiz_plan(plan, evidence)
        return plan

    monkeypatch.setattr(quiz_selection, "plan_lecture_quiz", planner)
    batches = quiz_selection.prepare_selected_batches(
        object(), "split-test", "unused", inputs, source_manifest(inputs),
        root=tmp_path / "selection", cancelled=lambda: False,
        on_lifecycle=lambda event: None, resume=False,
    )
    payloads = [json.loads(source) for _, source, _ in batches]
    # The initial 20+2 image split must move one question to the short final group.
    assert [len(payload["question_plan"]) for payload in payloads] == [19, 3]
    assert planning_calls == ["split-test:plan"]
    assert [question for payload in payloads for question in payload["question_plan"]] == (
        plan.model_dump(mode="json")["questions"]
    )
    for (_, _, selected_assets), payload in zip(batches, payloads, strict=True):
        assert 3 <= len(payload["question_plan"])
        assert len(payload["images"]) == len(selected_assets) <= MAX_BATCH_IMAGES
        planned_keys = {question["image"]["asset_key"] for question in payload["question_plan"]}
        assert {asset.key for asset in selected_assets} == planned_keys
        assert [image["asset_key"] for image in payload["images"]] == [
            asset.key for asset in selected_assets
        ]
        assert [image["input_index"] for image in payload["images"]] == list(
            range(len(selected_assets))
        )


def test_character_limit_finds_uneven_partition_when_midpoint_cannot_fit(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path)
    slides = inputs.documents[0]
    manifest = source_manifest(inputs)
    evidence = compact_evidence(inputs)
    evidence["sources"][1]["segments"][0]["text"] += "x" * (93_000 - len(_canonical(evidence)))
    assert len(_canonical(evidence)) == 93_000
    plan = QuizPlan.model_validate({
        "title": "Uneven question sizes",
        "questions": [{
            "id": f"q{index}",
            "focus": "x" * 2000 if index <= 4 else "Short focus",
            "objective_ids": [key for key, _ in inputs.objectives],
            "source_segments": [{"source_id": slides.source_id, "segment_key": "block-1"}],
            "image": {"source_id": slides.source_id, "asset_key": "figure-1"},
        } for index in range(1, 9)],
    })

    def planner(client, request_id, model, supplied, **kwargs):
        validate_quiz_plan(plan, supplied)
        return plan

    def prospective_size(questions):
        return len(_canonical({
            **evidence, "manifest_sha256": manifest["sha256"],
            "question_plan": [question.model_dump(mode="json") for question in questions],
            "images": [{**evidence["images"][0], "input_index": 0}],
        }))

    # A 4+4 midpoint fails; 3+5 preserves every question and fits both payloads.
    assert prospective_size(plan.questions[:4]) > MAX_SOURCE_CHARACTERS
    assert prospective_size(plan.questions[:3]) <= MAX_SOURCE_CHARACTERS
    assert prospective_size(plan.questions[3:]) <= MAX_SOURCE_CHARACTERS
    monkeypatch.setattr(quiz_selection, "compact_evidence", lambda _: deepcopy(evidence))
    monkeypatch.setattr(quiz_selection, "plan_lecture_quiz", planner)
    batches = quiz_selection.prepare_selected_batches(
        object(), "uneven-split", "unused", inputs, manifest,
        root=tmp_path / "selection", cancelled=lambda: False,
        on_lifecycle=lambda event: None, resume=False,
    )
    payloads = [json.loads(source) for _, source, _ in batches]
    assert [len(payload["question_plan"]) for payload in payloads] == [3, 5]
    assert all(len(source) <= MAX_SOURCE_CHARACTERS for _, source, _ in batches)
    assert [question for payload in payloads for question in payload["question_plan"]] == (
        plan.model_dump(mode="json")["questions"]
    )
    expected_sources = json.loads(_canonical(evidence["sources"]))
    assert all(payload["sources"] == expected_sources for payload in payloads)
    assert all([asset.key for asset in assets] == ["figure-1"] for _, _, assets in batches)


def test_character_partition_can_cross_preliminary_question_group_boundary(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path)
    slides = inputs.documents[0]
    manifest = source_manifest(inputs)
    evidence = compact_evidence(inputs)
    evidence["sources"][1]["segments"][0]["text"] += "x" * (96_300 - len(_canonical(evidence)))
    assert len(_canonical(evidence)) == 96_300
    assert len(evidence["images"]) == 1 and evidence["images"][0]["needs_preview"] is True
    plan = QuizPlan.model_validate({
        "title": "Partition across the preliminary boundary",
        "questions": [{
            "id": f"q{index}",
            "focus": "x" * 2000 if index in {20, 22} else "x",
            "objective_ids": [key for key, _ in inputs.objectives],
            "source_segments": [{"source_id": slides.source_id, "segment_key": "block-1"}],
            "image": None,
        } for index in range(26)],
    })

    def planner(client, request_id, model, supplied, **kwargs):
        validate_quiz_plan(plan, supplied)
        return plan

    def prospective_size(questions):
        return len(_canonical({
            **evidence, "manifest_sha256": manifest["sha256"],
            "question_plan": [question.model_dump(mode="json") for question in questions],
            "images": [{**evidence["images"][0], "input_index": 0}],
        }))

    # Fixing a 23+3 boundary traps both large plans in the first group's minimum
    # three-question tail. Moving the boundary allows a valid 13+9+4 partition.
    assert prospective_size(plan.questions[20:23]) > MAX_SOURCE_CHARACTERS
    assert all(prospective_size(group) <= MAX_SOURCE_CHARACTERS for group in (
        plan.questions[:13], plan.questions[13:22], plan.questions[22:],
    ))
    monkeypatch.setattr(quiz_selection, "compact_evidence", lambda _: deepcopy(evidence))
    monkeypatch.setattr(quiz_selection, "plan_lecture_quiz", planner)
    batches = quiz_selection.prepare_selected_batches(
        object(), "cross-group-split", "unused", inputs, manifest,
        root=tmp_path / "selection", cancelled=lambda: False,
        on_lifecycle=lambda event: None, resume=False,
    )
    payloads = [json.loads(source) for _, source, _ in batches]
    assert all(3 <= len(payload["question_plan"]) <= 25 for payload in payloads)
    assert all(len(source) <= MAX_SOURCE_CHARACTERS for _, source, _ in batches)
    assert [question for payload in payloads for question in payload["question_plan"]] == (
        plan.model_dump(mode="json")["questions"]
    )
    expected_sources = json.loads(_canonical(evidence["sources"]))
    assert all(payload["sources"] == expected_sources for payload in payloads)
    assert all(len(images) <= MAX_BATCH_IMAGES for _, _, images in batches)
    assert all([asset.key for asset in assets] == ["figure-1"] for _, _, assets in batches)
