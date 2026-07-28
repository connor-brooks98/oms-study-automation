from types import SimpleNamespace

import pytest

from oms_hub.config import Settings
from oms_hub.study_generation.google_docs import GoogleDocsGateway
from oms_hub.study_generation.native_quiz import QuizContractError


class UnexpectedDocs:
    def documents(self):
        raise AssertionError("untrusted link must be rejected before Google API access")


class Result:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class RecordingDocuments:
    def __init__(self):
        self.batch_bodies = []

    def get(self, **options):
        del options
        return Result(
            {
                "tabs": [
                    {
                        "tabProperties": {"tabId": "tab-1"},
                        "documentTab": {
                            "body": {"content": [{"endIndex": 2}]},
                            "namedRanges": {},
                        },
                    }
                ]
            }
        )

    def batchUpdate(self, **options):
        self.batch_bodies.append(options["body"])
        return Result({})


class RecordingDocs:
    def __init__(self):
        self.api = RecordingDocuments()

    def documents(self):
        return self.api


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


def test_google_docs_embeds_link_in_compact_lecture_quiz_label(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        public_hostname="study.example.com",
    )
    docs = RecordingDocs()
    gateway = GoogleDocsGateway(
        SimpleNamespace(),
        docs,
        SimpleNamespace(),
        settings,
    )
    tab = SimpleNamespace(document_id="doc-1", tab_id="tab-1")
    url = "https://study.example.com/public/quizzes/" + "a" * 64

    gateway.sync_quiz_link(tab, 2, url)

    requests = docs.api.batch_bodies[-1]["requests"]
    assert requests[0]["insertText"]["text"] == "Lecture 2 Quiz\n"
    assert requests[1] == {
        "updateTextStyle": {
            "range": {
                "tabId": "tab-1",
                "startIndex": 1,
                "endIndex": 15,
            },
            "textStyle": {"link": {"url": url}},
            "fields": "link",
        }
    }
    assert requests[2]["createNamedRange"]["range"]["endIndex"] == 16
