import json
from dataclasses import replace

import pytest

from oms_hub.study_generation.gpt_lecture import (
    GeneratedLectureQuiz,
    generate_lecture_quiz,
    validate_lecture_inputs,
)
from oms_hub.study_generation.quiz_selection import validate_planned_result
from tests.study_generation.test_gpt_lecture import _inputs, _quiz_payload, _QuizClient


def test_inventory_can_exceed_image_limit_while_only_selected_pixels_are_sent(tmp_path):
    inputs = _inputs(tmp_path)
    document = inputs.documents[0]
    rich = replace(document.segments[0], text=document.segments[0].text + ' Context.' * 20)
    extra = tuple(replace(document.assets[0], key=f'extra-{i}') for i in range(30))
    inputs = replace(inputs, prompt_version='gpt-lecture-v2', documents=(
        replace(document, segments=(rich, *document.segments[1:]),
                assets=(*document.assets, *extra)),
        inputs.documents[1],
    ))
    client = _QuizClient(tmp_path / 'work', inputs)
    quiz = generate_lecture_quiz(client, 'selection', 'chosen', inputs,
                                 cancelled=lambda: False, on_lifecycle=lambda e: None)
    assert len(quiz.questions) == 3
    assert len(client.requests) == 2
    planning, final = client.requests
    assert planning.request_id == 'selection:plan'
    assert planning.image_paths == ()
    assert len(json.loads(planning.source_text)['images']) == 31
    assert len(final.image_paths) == 1
    assert json.loads(final.source_text)['images'][0]['asset_key'] == 'figure-1'
    cached = generate_lecture_quiz(client, 'selection', 'chosen', inputs,
                                  cancelled=lambda: False, on_lifecycle=lambda e: None, resume=True)
    assert cached == quiz and len(client.requests) == 2


def test_final_output_cannot_introduce_unseen_images_or_change_count(tmp_path):
    inputs = _inputs(tmp_path)
    quiz = GeneratedLectureQuiz.model_validate(_quiz_payload(inputs))
    evidence = {'question_plan': [{'id': q.id, 'objective_ids': q.objective_ids}
                                 for q in quiz.questions], 'images': []}
    with pytest.raises(ValueError, match='not inspected'):
        validate_planned_result(quiz, json.dumps(evidence))
    evidence['images'] = [quiz.questions[0].image.model_dump()]
    validate_planned_result(quiz, json.dumps(evidence))
    with pytest.raises(ValueError, match='question plan'):
        validate_planned_result(quiz.model_copy(update={'questions': quiz.questions[:2]}),
                                json.dumps(evidence))


def test_only_recoverable_ocr_warnings_can_use_visual_inspection(tmp_path):
    inputs = _inputs(tmp_path)
    document = replace(inputs.documents[0], warnings=(
        'BLOCKER: OCR is required but unavailable or empty for slide 2',))
    inputs = replace(inputs, documents=(document, inputs.documents[1]))
    with pytest.raises(ValueError, match='BLOCKER'):
        validate_lecture_inputs(inputs)
    validate_lecture_inputs(replace(inputs, prompt_version='gpt-lecture-v2'))
    for warning in ('BLOCKER: corrupt archive',
                    'BLOCKER: OCR is required but unavailable or empty for slide 99'):
        with pytest.raises(ValueError, match='BLOCKER'):
            validate_lecture_inputs(replace(inputs, prompt_version='gpt-lecture-v2', documents=(
                replace(document, warnings=(warning,)), inputs.documents[1])))


def test_mixed_pdf_ocr_warning_uses_real_page_preview(tmp_path):
    from oms_hub.document_processing.domain import DocumentLocator

    inputs = _inputs(tmp_path)
    document = inputs.documents[0]
    page = DocumentLocator('page 2', page_number=2)
    document = replace(document, warnings=(
        'BLOCKER: OCR is required but unavailable or empty for page 2',),
        segments=tuple(replace(s, locator=page) for s in document.segments),
        assets=tuple(replace(a, locator=page, origin='full-page-render')
                     for a in document.assets))
    inputs = replace(inputs, prompt_version='gpt-lecture-v2',
                     documents=(document, inputs.documents[1]))
    validate_lecture_inputs(inputs)
    client = _QuizClient(tmp_path / 'work', inputs)
    generate_lecture_quiz(client, 'page-preview', 'chosen', inputs,
                          cancelled=lambda: False, on_lifecycle=lambda e: None)
    assert not client.requests[0].image_paths
    evidence = json.loads(client.requests[1].source_text)
    assert evidence['images'][0]['needs_preview']
    assert len(client.requests[1].image_paths) == 1
