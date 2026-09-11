"""Optional reference boundary; an acknowledgment does not establish developer access."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ReferencePassage:
    source_id: str
    title: str
    url: str
    text: str


class AmbossUnavailable(RuntimeError):
    pass


class AmbossReference:
    def search(self, question: str) -> tuple[ReferencePassage, ...]:
        raise AmbossUnavailable("AMBOSS access has not been configured")
