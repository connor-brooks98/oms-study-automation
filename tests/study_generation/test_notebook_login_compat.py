from types import SimpleNamespace

from oms_hub.study_generation.notebook_login_compat import (
    GEMINI_NOTEBOOK_HOST,
    _use_gemini_notebook_url,
)


class FakePage:
    def __init__(self):
        self.matcher = None

    def wait_for_url(self, matcher, **options):
        self.matcher = matcher
        return options


def test_login_compatibility_accepts_new_host_without_changing_api_host():
    login_module = SimpleNamespace(
        url_matches_base_host=lambda url: url.startswith(
            "https://notebooklm.google.com/"
        )
    )

    _use_gemini_notebook_url(login_module, FakePage)
    page = FakePage()
    result = page.wait_for_url(
        "https://notebooklm.google.com/**",
        wait_until="commit",
        timeout=300_000,
    )

    assert result == {"wait_until": "commit", "timeout": 300_000}
    assert callable(page.matcher)
    assert page.matcher("https://notebooklm.google.com/") is True
    assert page.matcher(f"https://{GEMINI_NOTEBOOK_HOST}/") is True
    assert page.matcher("https://notebook.google.com.evil.test/") is False
    assert login_module.url_matches_base_host(
        f"https://{GEMINI_NOTEBOOK_HOST}/"
    )
