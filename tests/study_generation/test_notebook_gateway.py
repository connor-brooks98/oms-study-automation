from dataclasses import dataclass

from oms_hub.study_generation.domain import (
    LectureSourceSet,
    NotebookRef,
    PromptSnapshot,
    RemoteSource,
    SourceKind,
)
from oms_hub.study_generation.notebook import NotebookLMGateway


@dataclass
class Answer:
    answer: str


class FakeChat:
    def __init__(self):
        self.calls = []

    async def ask(self, notebook_id, question, source_ids):
        self.calls.append(
            {
                "notebook_id": notebook_id,
                "question": question,
                "source_ids": source_ids,
            }
        )
        return Answer("Generated lecture material")


class FakeClient:
    def __init__(self):
        self.chat = FakeChat()


def test_ask_passes_exactly_pdf_and_transcript_source_ids(tmp_path):
    client = FakeClient()
    gateway = NotebookLMGateway(client)
    sources = LectureSourceSet(
        lecture_id=12,
        pdf=RemoteSource(
            "pdf-1", 12, 101, "a" * 64, SourceKind.LECTURE_PDF, True
        ),
        transcript=RemoteSource(
            "txt-1", 12, 202, "b" * 64, SourceKind.CLEANED_TRANSCRIPT, True
        ),
    )

    answer = gateway.ask(
        NotebookRef("nb-1", "Neuro · Exam 1"),
        sources,
        PromptSnapshot(tmp_path / "prompt.md", "Build the quiz", "c" * 64, "now"),
    )

    assert client.chat.calls == [
        {
            "notebook_id": "nb-1",
            "question": "Build the quiz",
            "source_ids": ["pdf-1", "txt-1"],
        }
    ]
    assert answer.text == "Generated lecture material"
