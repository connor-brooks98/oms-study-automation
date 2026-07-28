from types import SimpleNamespace

import pytest

from oms_hub.config import Settings
from oms_hub.study_generation.google_docs import GoogleDocsGateway
from oms_hub.study_generation.native_quiz import QuizContractError


class UnexpectedDocs:
    def documents(self):
        raise AssertionError("untrusted link must be rejected before Google API access")


def test_google_docs_rejects_quiz_links_outside_configured_study_hub(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        public_hostname="study.example.com",
    )
    gateway = GoogleDocsGateway(
        SimpleNamespace(),
        UnexpectedDocs(),
        SimpleNamespace(),
        settings,
    )
    tab = SimpleNamespace(document_id="doc-1", tab_id="tab-1")

    with pytest.raises(QuizContractError, match="untrusted"):
        gateway.sync_quiz_link(
            tab,
            1,
            "https://evil.example/public/quizzes/" + "a" * 64,
        )
