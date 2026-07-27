from typing import Protocol

from oms_hub.llm.domain import CleanResult
from oms_hub.transcripts.prompt import ApprovedPrompt


class TranscriptCleaner(Protocol):
    def clean(
        self,
        raw_text: str,
        prompt: ApprovedPrompt,
    ) -> CleanResult: ...
