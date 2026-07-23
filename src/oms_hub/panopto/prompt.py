import hashlib
from dataclasses import dataclass
from pathlib import Path

MAX_PROMPT_BYTES = 64 * 1024
STARTER_PROMPT = """# Transcript Cleaning

Remove verbal filler and false starts. Correct obvious transcription errors,
especially medical terminology, only when the intended wording is clear from
context. Add readable paragraphs and headings while preserving every
substantive fact, qualification, example, caution, and question.
"""


class PromptError(RuntimeError):
    pass


class PromptInvalid(PromptError):
    pass


class PromptNotApproved(PromptError):
    pass


@dataclass(frozen=True, slots=True)
class ApprovedPrompt:
    text: str
    sha256: str


class PromptLoader:
    def __init__(self, path: Path, approved_sha256: str | None):
        self.path = path
        self.approved_sha256 = approved_sha256

    def initialize(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("x", encoding="utf-8", newline="\n") as destination:
                destination.write(STARTER_PROMPT)
        except FileExistsError:
            pass
        return self.path

    def inspect(self) -> ApprovedPrompt:
        try:
            with self.path.open("rb") as source:
                payload = source.read(MAX_PROMPT_BYTES + 1)
        except OSError as error:
            raise PromptInvalid("Transcript cleaning prompt is not readable") from error
        if not payload or len(payload) > MAX_PROMPT_BYTES:
            raise PromptInvalid("Transcript cleaning prompt size is invalid")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PromptInvalid("Transcript cleaning prompt is not UTF-8") from error
        if not text.strip():
            raise PromptInvalid("Transcript cleaning prompt is empty")
        return ApprovedPrompt(text, hashlib.sha256(payload).hexdigest())

    def current(self) -> ApprovedPrompt:
        prompt = self.inspect()
        if not self.approved_sha256 or prompt.sha256 != self.approved_sha256:
            raise PromptNotApproved(
                "Transcript cleaning prompt has not been approved in its current form"
            )
        return prompt

