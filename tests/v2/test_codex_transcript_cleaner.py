import hashlib
import json
from dataclasses import replace

import pytest

from oms_hub.ingestion.domain import StudyRevision, UploadKind
from oms_hub.llm.codex_session import SessionError, SessionLifecycle, SessionResult
from oms_hub.transcripts.codex_cleaner import CodexTranscriptCleaner
from oms_hub.transcripts.prompt import ApprovedPrompt


def _revision(tmp_path):
    return StudyRevision(1, 'upload', 2, UploadKind.TRANSCRIPTS, 'a' * 64,
        tmp_path / 'raw.txt', None, tmp_path / 'clean.txt', None, None, None,
        None, 'processing', False)


class SessionFixture:
    def __init__(self, *, failure=None, output='{"text":"Preserved facts."}'):
        self.calls = 0
        self.failure = failure
        self.output = output

    def generate(self, request, *, cancelled, on_lifecycle):
        self.calls += 1
        if self.failure == 'preflight':
            raise SessionError('capability_unverified')
        for phase, thread, turn in [('dispatching', None, None),
            ('thread_created', 'thread', None), ('turn_started', 'thread', 'turn')]:
            on_lifecycle(SessionLifecycle(request.request_id, phase, thread, turn))
        if self.failure == 'interrupted':
            raise SessionError('interrupted')
        on_lifecycle(SessionLifecycle(request.request_id, 'completed', 'thread', 'turn'))
        return SessionResult('thread', 'turn', self.output)


def test_completed_cleaning_is_cached_and_bound_to_revision_and_prompt(tmp_path):
    session = SessionFixture()
    cleaner = CodexTranscriptCleaner(session, 'fixture')
    revision = _revision(tmp_path)
    prompt = ApprovedPrompt('Preserve facts', 'b' * 64)
    first = cleaner.clean_revision('Raw facts', prompt, revision)
    assert first.provider == 'codex_subscription'
    assert cleaner.clean_revision('Raw facts', prompt, revision) == first
    assert session.calls == 1
    for changed_revision, changed_prompt, raw in [
        (replace(revision, source_sha256='c' * 64), prompt, 'Raw facts'),
        (revision, ApprovedPrompt('Changed', 'd' * 64), 'Raw facts'),
        (revision, prompt, 'Different source'),
    ]:
        with pytest.raises(SessionError, match='interrupted'):
            cleaner.clean_revision(raw, changed_prompt, changed_revision)
    assert session.calls == 1
    journal = json.loads((tmp_path / 'clean.gpt-attempt.json').read_text())
    assert journal['result_sha256'] == hashlib.sha256(first.text.encode()).hexdigest()


@pytest.mark.parametrize('failure,output', [('interrupted', '{}'), (None, '{"bad":1}')])
def test_ambiguous_or_invalid_output_never_replays(tmp_path, failure, output):
    session = SessionFixture(failure=failure, output=output)
    cleaner = CodexTranscriptCleaner(session, 'fixture')
    args = ('Raw facts', ApprovedPrompt('Preserve', 'b' * 64), _revision(tmp_path))
    with pytest.raises(SessionError):
        cleaner.clean_revision(*args)
    with pytest.raises(SessionError, match='interrupted'):
        cleaner.clean_revision(*args)
    assert session.calls == 1


def test_no_dispatch_failure_retains_receipt_and_allows_explicit_retry(tmp_path):
    session = SessionFixture(failure='preflight')
    cleaner = CodexTranscriptCleaner(session, 'fixture')
    args = ('Raw facts', ApprovedPrompt('Preserve', 'b' * 64), _revision(tmp_path))
    with pytest.raises(SessionError, match='not verified'):
        cleaner.clean_revision(*args)
    assert len(list(tmp_path.glob('*.blocked-*.json'))) == 1
    assert not (tmp_path / 'clean.gpt-attempt.json').exists()
    session.failure = None
    assert cleaner.clean_revision(*args).text == 'Preserved facts.'
    assert session.calls == 2
