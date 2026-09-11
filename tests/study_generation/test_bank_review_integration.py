import json

import pytest
from sqlalchemy import select

from oms_hub.db import Database
from oms_hub.models import (
    BankImportModel,
    BankImportRowModel,
    BankQuestionModel,
    BankReviewQuestionModel,
    PublishedQuizModel,
)
from oms_hub.study_generation.practice_domain import (
    AnswerProvenance,
    QuestionDraft,
    QuestionSourceRef,
)
from oms_hub.study_generation.practice_review import PracticeReviewService
from oms_hub.study_generation.studio_repository import StudioRepository


def test_bank_review_is_atomic_owned_and_not_published(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    with database.session() as session:
        session.add(BankImportModel(id='import', learner_id='owner', source='user', product='notes',
            export_id='export', digest='a' * 64,
            provenance_json=json.dumps({'kind': 'authorized_question_export'})))
        question = BankQuestionModel(source='user', product='notes', external_question_id='00123')
        session.add(question)
        session.flush()
        session.add(BankImportRowModel(import_id='import', row_number=1, question_id=question.id,
            canonical_row_hash='b' * 64, row_json=json.dumps({'question': {'stem': 'Case'}})))
    repo = StudioRepository(database)
    draft = QuestionDraft('q1', 'user/notes/00123', 'Case', ('A', 'B'), 0, 'Evidence', None,
        (QuestionSourceRef('import', 'row:1', 'Import row 1'),),
        AnswerProvenance.PROVIDED_BY_SOURCE, 1.0, (), True, None)
    kwargs = dict(bank_import_id='import', learner_id='owner', subject='Heme', exam_number=3,
        label='Practice', drafts=(draft,))
    with pytest.raises(ValueError, match='ownership'):
        repo.create_bank_import_review(**(kwargs | {'learner_id': 'other'}))
    run = repo.create_bank_import_review(**kwargs)
    assert run.state.value == 'awaiting_review'
    assert repo.create_bank_import_review(**kwargs).id == run.id
    assert repo.claim_next_run() is None
    review = PracticeReviewService(repo)
    assert review.question(run.id, 'q1').verification_required
    with pytest.raises(ValueError):
        review.to_native_quiz(run.id)
    assert review.verify_generated_answer(run.id, 'q1').verified_at
    assert len(review.to_native_quiz(run.id).questions) == 1
    with database.session() as session:
        assert session.scalar(select(PublishedQuizModel)) is None
        link = session.scalar(select(BankReviewQuestionModel))
        assert link is not None and link.local_question_id == 'q1'
    database.close()
