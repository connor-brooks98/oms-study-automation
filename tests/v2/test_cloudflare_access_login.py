from pathlib import Path

LOGIN_PAGE = (
    Path(__file__).parents[2] / "deploy" / "cloudflare" / "access-login.html"
)


def test_access_login_preview_is_school_agnostic_and_accessible() -> None:
    page = LOGIN_PAGE.read_text(encoding="utf-8")

    assert "Study Hub" in page
    assert "Please enter school email to generate a OTP" in page
    assert "autocomplete=\"email\"" in page
    assert "autocomplete=\"one-time-code\"" in page
    assert page.count("class=\"otp-slot\"") == 6
    assert "grid-column: 2" in page
    assert "@keyframes otp-success" in page
    assert 'window.location.assign(successUrl)' in page
    assert "prefers-reduced-motion: reduce" in page
    assert "aria-live=\"polite\"" in page
    assert "LMU" not in page
    assert "lmunet" not in page.lower()
    assert "fetch(" not in page
    assert "Code ready for secure verification" not in page
