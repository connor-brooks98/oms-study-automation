import pytest

from oms_hub.study_generation.gemini_quiz import (
    GeminiContractError,
    validate_shared_quiz_url,
)


def test_valid_gemini_share_url_is_normalized():
    assert (
        validate_shared_quiz_url("https://gemini.google.com/share/quiz-123")
        == "https://gemini.google.com/share/quiz-123"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://gemini.google.com/share/x",
        "https://evil.example/x",
        "javascript:alert(1)",
        "https://gemini.google.com/app/x",
    ],
)
def test_gateway_rejects_untrusted_share_urls(url):
    with pytest.raises(GeminiContractError):
        validate_shared_quiz_url(url)
