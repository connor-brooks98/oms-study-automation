"""Public grounded-learning provider contracts and deterministic fakes."""

from oms_hub.providers.contracts import (
    AnswerEvent,
    AnswerEventType,
    AuthorityClass,
    EvidenceRef,
    GroundedAnswerProvider,
    GroundedAnswerRequest,
    ProviderHealth,
    RetrievalProvider,
    RetrievalRequest,
    RetrievalResult,
    RetrievalScope,
    TruthMode,
)
from oms_hub.providers.fake import FakeGroundedAnswerProvider, FakeRetrievalProvider
from oms_hub.providers.registry import ProviderRegistry

__all__ = [
    "AnswerEvent",
    "AnswerEventType",
    "AuthorityClass",
    "EvidenceRef",
    "FakeGroundedAnswerProvider",
    "FakeRetrievalProvider",
    "GroundedAnswerProvider",
    "GroundedAnswerRequest",
    "ProviderHealth",
    "ProviderRegistry",
    "RetrievalProvider",
    "RetrievalRequest",
    "RetrievalResult",
    "RetrievalScope",
    "TruthMode",
]
