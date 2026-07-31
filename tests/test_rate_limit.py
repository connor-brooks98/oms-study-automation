from oms_hub.security.rate_limit import (
    PublicQuizRateLimiter,
    RatePolicy,
    public_client_identifier,
)


def test_token_bucket_refills_and_keeps_clients_separate():
    now = [100.0]
    limiter = PublicQuizRateLimiter(
        general_client=RatePolicy(1, 1),
        general_global=RatePolicy(10, 10),
        clock=lambda: now[0],
    )

    assert limiter.check("first").allowed is True
    limited = limiter.check("first")
    assert limited.allowed is False
    assert limited.retry_after_seconds == 1
    assert limiter.check("second").allowed is True

    now[0] += 1
    assert limiter.check("first").allowed is True


def test_outline_policy_is_independent_and_stricter():
    limiter = PublicQuizRateLimiter(
        general_client=RatePolicy(5, 0),
        general_global=RatePolicy(10, 0),
        outline_client=RatePolicy(1, 0),
        outline_global=RatePolicy(10, 0),
    )

    assert limiter.check("student", "general").allowed is True
    assert limiter.check("student", "general").allowed is True
    assert limiter.check("student", "outline").allowed is True
    assert limiter.check("student", "outline").allowed is False


def test_public_client_identifier_accepts_only_valid_forwarded_addresses():
    assert public_client_identifier("203.0.113.4", "127.0.0.1") == "203.0.113.4"
    assert public_client_identifier("not-an-ip", "127.0.0.1") == "127.0.0.1"
