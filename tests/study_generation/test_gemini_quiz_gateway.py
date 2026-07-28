import pytest

from oms_hub.study_generation.gemini_quiz import (
    GeminiContractError,
    GeminiQuizGateway,
    GeminiQuizRef,
    validate_shared_quiz_url,
)


class _Locator:
    def __init__(self, matches=(), *, input_value=""):
        self.matches = tuple(matches)
        self.last = self
        self.value = ""
        self._input_value = input_value

    def count(self):
        return len(self.matches)

    def wait_for(self, **kwargs):
        del kwargs
        if len(self.matches) != 1:
            raise RuntimeError(
                f"strict locator expected one match, found {len(self.matches)}"
            )

    def fill(self, value):
        self.value = value

    def press(self, key):
        if key != "Enter":
            raise AssertionError(f"unexpected key: {key}")

    def click(self):
        if len(self.matches) != 1:
            raise RuntimeError(
                f"strict locator expected one match, found {len(self.matches)}"
            )

    def input_value(self):
        return self._input_value


class _GeminiQuizPage:
    def __init__(self):
        self.url = "https://gemini.google.com/gem/example"
        self.editor = _Locator(("prompt editor",))

    def goto(self, url, **kwargs):
        del kwargs
        self.url = url

    def get_by_text(self, text, **kwargs):
        del text, kwargs
        return _Locator()

    def get_by_role(self, role, name=None, exact=False):
        if role == "textbox":
            return self.editor
        if role != "button":
            return _Locator()
        available = ("Share", "Share quiz")
        matches = (
            button
            for button in available
            if (button == name if exact else name in button)
        )
        return _Locator(matches)

    def locator(self, selector):
        if selector == 'input[type="url"]':
            return _Locator(
                ("shared quiz URL",),
                input_value="https://gemini.google.com/share/quiz-123",
            )
        return _Locator()


class _TimeoutLocator(_Locator):
    def wait_for(self, **kwargs):
        del kwargs
        raise TimeoutError("Share quiz was not found")


class _MissingQuizSharePage(_GeminiQuizPage):
    def get_by_role(self, role, name=None, exact=False):
        if role == "button" and name == "Share quiz":
            return _TimeoutLocator()
        return super().get_by_role(role, name, exact)


def test_generate_uses_the_quiz_specific_share_control():
    page = _GeminiQuizPage()
    gateway = GeminiQuizGateway(
        page,
        "https://gemini.google.com/gem/example",
    )

    quiz = gateway.generate("job-123", "Question 1")

    assert quiz.id == "https://gemini.google.com/gem/example"
    assert page.editor.value == "[OMS Study Hub job job-123]\n\nQuestion 1"


def test_generate_reports_when_gemini_does_not_show_the_quiz_share_control():
    gateway = GeminiQuizGateway(
        _MissingQuizSharePage(),
        "https://gemini.google.com/gem/example",
    )

    with pytest.raises(GeminiContractError, match="could not find Share quiz"):
        gateway.generate("job-123", "Question 1")


def test_share_uses_the_quiz_specific_share_control():
    page = _GeminiQuizPage()
    gateway = GeminiQuizGateway(
        page,
        "https://gemini.google.com/gem/example",
    )

    shared = gateway.share(GeminiQuizRef("https://gemini.google.com/app/quiz-123"))

    assert shared.url == "https://gemini.google.com/share/quiz-123"


def test_valid_gemini_share_url_is_normalized():
    assert (
        validate_shared_quiz_url("https://gemini.google.com/share/quiz-123")
        == "https://gemini.google.com/share/quiz-123"
    )


def test_google_short_quiz_share_url_is_accepted():
    assert (
        validate_shared_quiz_url("https://g.co/gemini/share/quiz-123")
        == "https://g.co/gemini/share/quiz-123"
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
