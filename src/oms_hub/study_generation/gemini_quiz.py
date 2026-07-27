from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from oms_hub.study_generation.browser_profile import launch_google_profile


class GeminiContractError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GeminiQuizRef:
    id: str


@dataclass(frozen=True, slots=True)
class SharedQuiz:
    url: str


def validate_shared_quiz_url(url: str) -> str:
    normalized = url.strip()
    parsed = urlparse(normalized)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "gemini.google.com"
        or not parsed.path.startswith("/share/")
        or len(parsed.path) <= len("/share/")
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise GeminiContractError("Gemini returned an untrusted quiz link")
    return normalized


class GeminiQuizGateway:
    """Locator-driven adapter for the user's stable Gemini Quiz Gem."""

    def __init__(
        self,
        page: Any,
        configured_gem_url: str,
        *,
        timeout_ms: int = 180_000,
    ):
        parsed = urlparse(configured_gem_url)
        if parsed.scheme != "https" or parsed.hostname != "gemini.google.com":
            raise ValueError("Gemini Quiz Gem URL must use gemini.google.com")
        self.page = page
        self.configured_gem_url = configured_gem_url
        self.timeout_ms = timeout_ms

    def generate(self, job_id: str, quiz_content: str) -> GeminiQuizRef:
        if not quiz_content.strip():
            raise GeminiContractError("NotebookLM quiz content was empty")
        marker = f"[OMS Study Hub job {job_id}]"
        self.page.goto(self.configured_gem_url, wait_until="domcontentloaded")
        if self.page.get_by_text(marker, exact=False).count():
            return GeminiQuizRef(str(self.page.url))
        editor = self.page.get_by_role("textbox").last
        editor.wait_for(state="visible", timeout=self.timeout_ms)
        editor.fill(f"{marker}\n\n{quiz_content}")
        editor.press("Enter")
        self.page.get_by_role("button", name="Share").wait_for(
            state="visible",
            timeout=self.timeout_ms,
        )
        return GeminiQuizRef(str(self.page.url))

    def share(self, quiz: GeminiQuizRef) -> SharedQuiz:
        del quiz
        self.page.get_by_role("button", name="Share").click()
        anyone = self.page.get_by_text("Anyone with the link", exact=False)
        if anyone.count():
            anyone.first.click()
        link = self.page.locator('input[type="url"]').last
        link.wait_for(state="visible", timeout=self.timeout_ms)
        value = link.input_value()
        return SharedQuiz(validate_shared_quiz_url(value))


class PersistentGeminiQuizGateway:
    def __init__(
        self,
        profile_path: Path,
        configured_gem_url: str | None,
        *,
        timeout_ms: int,
    ):
        self.profile_path = profile_path
        self.configured_gem_url = configured_gem_url
        self.timeout_ms = timeout_ms

    def generate(self, job_id: str, quiz_content: str) -> GeminiQuizRef:
        with self._page() as page:
            return GeminiQuizGateway(
                page,
                self._gem_url(),
                timeout_ms=self.timeout_ms,
            ).generate(job_id, quiz_content)

    def share(self, quiz: GeminiQuizRef) -> SharedQuiz:
        with self._page() as page:
            page.goto(quiz.id, wait_until="domcontentloaded")
            return GeminiQuizGateway(
                page,
                self._gem_url(),
                timeout_ms=self.timeout_ms,
            ).share(quiz)

    def _gem_url(self) -> str:
        if not self.configured_gem_url:
            raise GeminiContractError(
                "Configure the Gemini Quiz Gem URL before generating quizzes"
            )
        return self.configured_gem_url

    def _page(self) -> Any:
        return _PersistentPage(self.profile_path)


class _PersistentPage:
    def __init__(self, profile_path: Path):
        self.profile_path = profile_path

    def __enter__(self) -> Any:
        from playwright.sync_api import sync_playwright

        self.playwright = sync_playwright().start()
        self.context = launch_google_profile(
            self.playwright.chromium,
            self.profile_path,
            headless=True,
        )
        return self.context.pages[0] if self.context.pages else self.context.new_page()

    def __exit__(self, *args: object) -> None:
        self.context.close()
        self.playwright.stop()
