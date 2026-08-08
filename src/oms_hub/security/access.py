import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import jwt


class AccessTokenInvalid(Exception):
    """The Cloudflare Access assertion could not be authenticated."""


class AccessIdentityForbidden(Exception):
    """The assertion is valid, but belongs to an unauthorized identity."""


class JwksClient(Protocol):
    def get_signing_key_from_jwt(self, assertion: str) -> Any: ...


@dataclass(frozen=True)
class AccessIdentity:
    email: str
    subject: str
    issued_at: datetime
    expires_at: datetime


class CloudflareAccessVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        allowed_email: str,
        jwks_client: JwksClient | None = None,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.allowed_email = allowed_email.casefold()
        self.jwks_client = jwks_client or jwt.PyJWKClient(
            f"{self.issuer}/cdn-cgi/access/certs",
            cache_jwk_set=True,
            lifespan=3600,
        )

    def verify(self, assertion: str) -> AccessIdentity:
        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(assertion).key
            claims = jwt.decode(
                assertion,
                signing_key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "email"]},
            )
        except jwt.PyJWTError as exc:
            raise AccessTokenInvalid("Cloudflare Access assertion is invalid") from exc
        except Exception as exc:
            raise AccessTokenInvalid("Cloudflare Access keys are unavailable") from exc

        email = str(claims["email"]).strip()
        if email.casefold() != self.allowed_email:
            raise AccessIdentityForbidden("Cloudflare Access identity is not allowed")
        return AccessIdentity(
            email=email,
            subject=str(claims["sub"]),
            issued_at=datetime.fromtimestamp(float(claims["iat"]), UTC),
            expires_at=datetime.fromtimestamp(float(claims["exp"]), UTC),
        )


def bearer_token_is_valid(
    authorization: str | None,
    expected_token: str | None,
) -> bool:
    if not authorization or not expected_token:
        return False
    scheme, separator, submitted = authorization.partition(" ")
    if separator != " " or scheme != "Bearer" or not submitted:
        return False
    return hmac.compare_digest(submitted, expected_token)
