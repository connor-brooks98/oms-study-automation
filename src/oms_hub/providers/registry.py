"""Instance-local registry for grounded-learning providers."""

from oms_hub.providers.contracts import GroundedAnswerProvider, RetrievalProvider


class ProviderRegistry:
    """Keep explicitly configured retrieval and answer providers separate."""

    def __init__(self) -> None:
        self._retrieval_providers: dict[str, RetrievalProvider] = {}
        self._answer_providers: dict[str, GroundedAnswerProvider] = {}

    def register_retrieval(self, name: str, provider: RetrievalProvider) -> None:
        self._register(self._retrieval_providers, "retrieval", name, provider)

    def register_answer(self, name: str, provider: GroundedAnswerProvider) -> None:
        self._register(self._answer_providers, "answer", name, provider)

    def get_retrieval(self, name: str) -> RetrievalProvider:
        return self._get(self._retrieval_providers, "retrieval", name)

    def get_answer(self, name: str) -> GroundedAnswerProvider:
        return self._get(self._answer_providers, "answer", name)

    @staticmethod
    def _register[T](
        providers: dict[str, T], category: str, name: str, provider: T
    ) -> None:
        if not name.strip():
            raise ValueError("provider name must not be blank")
        if name in providers:
            raise ValueError(f"{category} provider already registered: {name}")
        providers[name] = provider

    @staticmethod
    def _get[T](providers: dict[str, T], category: str, name: str) -> T:
        try:
            return providers[name]
        except KeyError:
            registered = ", ".join(sorted(providers)) or "none"
            raise KeyError(
                f"{category} provider not registered: {name}; registered: {registered}"
            ) from None
