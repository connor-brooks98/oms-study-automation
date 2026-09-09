from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPrivateKey,
    RSAPublicKey,
    generate_private_key,
)

from oms_hub.security.access import AccessTokenInvalid, CloudflareAccessVerifier

ISSUER = "https://study.cloudflareaccess.com"
AUDIENCE = "owner-audience"
EMAIL = "connor@example.com"


class StaticJwksClient:
    def __init__(self, key: RSAPublicKey) -> None:
        self.key = key

    def get_signing_key_from_jwt(self, assertion: str) -> Any:
        del assertion
        return SimpleNamespace(key=self.key)


class FailingJwksClient:
    def get_signing_key_from_jwt(self, assertion: str) -> Any:
        del assertion
        raise jwt.PyJWKClientConnectionError("sensitive upstream detail")


@pytest.fixture(scope="module")
def signing_keys() -> tuple[RSAPrivateKey, RSAPublicKey]:
    private_key = generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _verifier(public_key: RSAPublicKey) -> CloudflareAccessVerifier:
    return CloudflareAccessVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        allowed_email=EMAIL,
        jwks_client=StaticJwksClient(public_key),
    )


def _assertion(
    private_key: RSAPrivateKey,
    *,
    issued_at: int,
    expires_at: int,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
) -> str:
    return jwt.encode(
        {
            "iss": issuer,
            "aud": [audience],
            "sub": "owner-subject",
            "email": EMAIL,
            "iat": issued_at,
            "exp": expires_at,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "owner-key"},
    )


def test_access_verifier_accepts_small_issued_at_clock_skew(
    signing_keys: tuple[RSAPrivateKey, RSAPublicKey],
) -> None:
    private_key, public_key = signing_keys
    now = int(datetime.now(UTC).timestamp())

    identity = _verifier(public_key).verify(
        _assertion(private_key, issued_at=now + 4, expires_at=now + 300)
    )

    assert identity.email == EMAIL
    assert identity.subject == "owner-subject"


@pytest.mark.parametrize(
    ("assertion_kind", "reason"),
    [
        ("not_yet_valid", "not_yet_valid"),
        ("expired", "expired"),
        ("wrong_audience", "invalid_audience"),
        ("wrong_issuer", "invalid_issuer"),
        ("wrong_signature", "invalid_signature"),
    ],
)
def test_access_verifier_reports_safe_invalid_token_reason(
    signing_keys: tuple[RSAPrivateKey, RSAPublicKey],
    assertion_kind: str,
    reason: str,
) -> None:
    private_key, public_key = signing_keys
    now = int(datetime.now(UTC).timestamp())
    other_key = generate_private_key(public_exponent=65537, key_size=2048)
    cases = {
        "not_yet_valid": _assertion(private_key, issued_at=now + 10, expires_at=now + 300),
        "expired": _assertion(private_key, issued_at=now - 300, expires_at=now - 10),
        "wrong_audience": _assertion(
            private_key,
            issued_at=now - 1,
            expires_at=now + 300,
            audience="public-audience",
        ),
        "wrong_issuer": _assertion(
            private_key,
            issued_at=now - 1,
            expires_at=now + 300,
            issuer="https://other.cloudflareaccess.com",
        ),
        "wrong_signature": _assertion(
            other_key,
            issued_at=now - 1,
            expires_at=now + 300,
        ),
    }

    with pytest.raises(AccessTokenInvalid) as raised:
        _verifier(public_key).verify(cases[assertion_kind])

    assert raised.value.reason == reason
    assert str(raised.value) == "Cloudflare Access assertion is invalid"


def test_access_verifier_prioritizes_jwks_connection_failure() -> None:
    verifier = CloudflareAccessVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        allowed_email=EMAIL,
        jwks_client=FailingJwksClient(),
    )

    with pytest.raises(AccessTokenInvalid) as raised:
        verifier.verify("sentinel-private-jwt")

    assert raised.value.reason == "jwks_unavailable"
    assert "sensitive upstream detail" not in str(raised.value)
