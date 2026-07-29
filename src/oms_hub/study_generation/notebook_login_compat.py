"""Compatibility entry point for Google's Gemini Notebook login host."""

from typing import Any
from urllib.parse import urlparse

GEMINI_NOTEBOOK_HOST = "notebook.google.com"
LEGACY_NOTEBOOK_HOST = "notebooklm.google.com"
LEGACY_LOGIN_PATTERN = f"https://{LEGACY_NOTEBOOK_HOST}/**"


def _is_gemini_notebook_url(url: object) -> bool:
    return (urlparse(str(url)).hostname or "").casefold() == GEMINI_NOTEBOOK_HOST


def _use_gemini_notebook_url(login_module: Any, page_type: Any) -> None:
    """Backport the upstream Gemini Notebook login-host recognition fix."""

    original_matches_base_host = login_module.url_matches_base_host
    original_wait_for_url = page_type.wait_for_url

    def matches_notebook_host(url: object) -> bool:
        return bool(
            original_matches_base_host(str(url))
            or _is_gemini_notebook_url(url)
        )

    def wait_for_notebook_url(
        page: Any,
        matcher: Any,
        *arguments: Any,
        **options: Any,
    ) -> Any:
        if matcher == LEGACY_LOGIN_PATTERN:
            matcher = matches_notebook_host
        return original_wait_for_url(page, matcher, *arguments, **options)

    login_module.url_matches_base_host = matches_notebook_host
    page_type.wait_for_url = wait_for_notebook_url


def main() -> None:
    from notebooklm.cli.services import playwright_login
    from notebooklm.notebooklm_cli import main as notebooklm_main
    from playwright.sync_api import Page

    _use_gemini_notebook_url(playwright_login, Page)
    notebooklm_main()  # type: ignore[no-untyped-call]


if __name__ == "__main__":
    main()
