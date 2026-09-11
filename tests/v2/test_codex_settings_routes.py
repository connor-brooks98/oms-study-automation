from types import SimpleNamespace

from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.llm.codex_session import LoginChallenge, SessionStatus


class ManagedFixture:
    def __init__(self, *args, **kwargs):
        self.calls = []

    def start_login(self):
        self.calls.append('login')
        return LoginChallenge('fixture-login', 'https://auth.openai.com/codex/device', 'TEST-CODE')

    def status(self):
        self.calls.append('status')
        return SessionStatus('unavailable', ('fixture-model',), (), None,
            'capability_unverified', account_connected=True)

    def cancel_login(self, login_id):
        self.calls.append(('cancel', login_id))

    def close(self):
        self.calls.append('close')


def test_managed_settings_are_explicit_private_and_share_one_client(tmp_path, monkeypatch):
    monkeypatch.setattr('oms_hub.app.CodexSessionClient', ManagedFixture)
    settings = Settings(_env_file=None, data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}", codex_executable=tmp_path / 'codex',
        codex_binary_sha256='a' * 64)
    app = create_app(settings)
    session = app.state.codex_session
    assert session.calls == []
    assert app.state.gpt_transcript_cleaner.client is session
    assert app.state.ingestion_worker.gpt_transcript_pipeline is app.state.gpt_transcript_pipeline
    app.state.worker_supervisor = SimpleNamespace(start=lambda: None, stop=lambda: None)
    with TestClient(app) as client:
        client.get('/settings')
        assert session.calls == []
        assert client.post('/settings/generation/codex/login').status_code == 403
        headers = {'X-CSRF-Token': client.cookies['study_hub_csrf']}
        login = client.post('/settings/generation/codex/login', headers=headers)
        assert login.status_code == 200 and login.json()['user_code'] == 'TEST-CODE'
        response = client.post('/settings/generation/codex/status', headers=headers)
        assert response.json()['account_connected'] is True
        assert response.json()['state'] == 'unavailable'
        assert response.json()['image_model_ids'] == []
        assert 'session_home' not in response.text
        response = client.post('/settings/generation/codex/model', headers=headers,
            json={'model': 'fixture-model'})
        assert response.status_code == 200
        assert app.state.study_ai_settings.get().codex_model == 'fixture-model'
        assert app.state.gpt_transcript_cleaner.model == 'fixture-model'
        assert app.state.gpt_lecture_service.model == 'fixture-model'
        assert client.post('/settings/generation/codex/model', headers=headers,
            json={'model': 'not-advertised'}).status_code == 409
    assert session.calls[-1] == 'close'
    restored = create_app(settings)
    assert restored.state.codex_model == 'fixture-model'
    restored.state.database.close()


def test_gpt_progress_controls_require_owner_and_csrf(tmp_path):
    from oms_hub.models import StudioRunModel

    app = create_app(Settings(_env_file=None, data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}"))
    with app.state.database.session() as session:
        session.add(StudioRunModel(id='run', subject='Heme', subject_key='heme', exam_number=3,
            destination_subject='Heme', destination_subject_key='heme', destination_exam_number=3,
            label='Quiz', prompt='', backend='codex_subscription', state='paused'))
    repo = app.state.studio_repository
    repo.save_run_artifact('run', 'gpt:settings', 'hash', '{"owner_id":"local-owner"}')
    client = TestClient(app)
    page = client.get('/lectures/gpt-runs/run')
    assert page.status_code == 200 and 'Quiz progress' in page.text
    path = '/settings/generation/codex/runs/run'
    assert client.get(path).json()['state'] == 'paused'
    assert client.post(path + '/resume').status_code == 403
    headers = {'X-CSRF-Token': client.cookies['study_hub_csrf']}
    assert client.post(path + '/resume', headers=headers).json()['state'] == 'queued'
    assert client.post(path + '/cancel', headers=headers).json()['state'] == 'interrupted'
    repo.save_run_artifact('run', 'gpt:settings', 'hash', '{"owner_id":"another-owner"}')
    assert client.get(path).status_code == 404
    assert client.post(path + '/resume', headers=headers).status_code == 409
    app.state.database.close()
