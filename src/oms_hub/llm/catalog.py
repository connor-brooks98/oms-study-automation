from collections.abc import Mapping

from oms_hub.llm.domain import ProviderName

FALLBACK_MODELS: Mapping[ProviderName, tuple[str, ...]] = {
    ProviderName.OPENAI: (
        "gpt-5.2",
        "gpt-5.2-mini",
        "gpt-5.1",
        "gpt-4.1",
    ),
    ProviderName.ANTHROPIC: (
        "claude-fable-5",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
    ),
    ProviderName.GEMINI: (
        "gemini-3-pro",
        "gemini-3-flash",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
    ),
    ProviderName.OPENROUTER: (
        "openai/gpt-4o-mini",
        "google/gemini-2.0-flash-001",
        "anthropic/claude-3.5-sonnet",
        "openrouter/free",
    ),
}
