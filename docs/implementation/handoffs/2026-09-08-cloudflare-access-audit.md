# Cloudflare Access audit — 2026-09-08

The initial audit below was read-only. The subsequent clock diagnosis and origin fix are recorded at the end. Cloudflare dashboard policies, audiences, identity providers, and cookie settings remain unchanged. The exact URL and token behind the original error have not been captured.

## Verified live configuration

The Cloudflare Access team domain is `hidden-dew-30be.cloudflareaccess.com`. The NUC origin uses that same value as its configured issuer.

| Scope | Access application and route | Policies | Session and cookie settings | Audience |
| --- | --- | --- | --- | --- |
| Owner/private | `7e47acb5-6a6a-456f-bf47-4d771f4732bc`, root `studyhub.perch-bird.com` | `PersonalAccess` allows only `conbro13@gmail.com`; this matches the NUC origin's configured allowed email | App session 24 hours; policy inherits the app session; cookie path off | `231ed0435aa9d8bb59d47bf13e278df37e752d436bec85c1cb653663ab67296a`, an exact match for the NUC origin's configured audience |
| Public/school | `30801d68-054a-4015-b7db-91479473d2c8`, path `public*` | `LMUEmailLogin` allows `@lmunet.edu` with a one-month policy; `PersonalAccess` also applies | App session one month; cookie path on | `e9b011eae2e40841c05bfcd91005ac692b474431861ce00ed8856aa0b61cdd73` |

The global session duration is one month. Both applications currently have HttpOnly off and Binding off. Those settings were observed, but there is no evidence that either caused the reported error. Neither application has a Bypass or Everyone policy.

At `2026-09-09T00:43:28Z`, the NUC could retrieve the team's JWKS endpoint with HTTP 200 and received two keys.

Cloudflare selects the more-specific application path for matching requests. Consequently, the school email policy applies to the public application, while the owner/private root application allows the Gmail owner identity. Cloudflare documents application path precedence in [Access application paths](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/app-paths/) and the per-application authorization cookie in [Access authorization cookies](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/).

## What the evidence establishes

The owner application's issuer, audience, and allowed email agree with the NUC origin configuration. The public application's separate audience and school policy also agree with the intended public-only access boundary. These facts do not establish the cause of a prior `Cloudflare Access identity is invalid` response, and they do not currently justify a Cloudflare configuration or application-code change.

The home page loaded, and the public quiz library loaded. A browser navigation back to the root later produced Chrome `ERR_BLOCKED_BY_CLIENT`. That browser-local failure is inconclusive and is not the origin response `Cloudflare Access identity is invalid`. The exact failing URL, timestamp, HTTP status, and response body remain the most useful optional follow-up evidence.

## Origin response mapping

The origin handles private-path Access checks in [`app.py`](../../../src/oms_hub/app.py#L742) and verifies signed assertions in [`access.py`](../../../src/oms_hub/security/access.py#L47).

| HTTP response | Origin detail | Meaning in the current code |
| --- | --- | --- |
| 503 | `Cloudflare Access is not configured` | The origin has no Access verifier. |
| 401 | `Cloudflare Access identity is required` | `Cf-Access-Jwt-Assertion` is absent. |
| 401 | `Cloudflare Access identity is invalid` | Assertion authentication failed, including signature, issuer, audience, expiry, required claims, or JWKS retrieval. |
| 403 | `Cloudflare Access identity is not allowed` | The assertion validated, but its email did not exactly match the configured allowed email after case folding. |

The verifier accepts only RS256 and requires `exp`, `iat`, `iss`, `aud`, `sub`, and `email`. Cloudflare's corresponding requirements and JWKS endpoint are documented in [Validate JSON Web Tokens](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/) and [Application token claims](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/application-token/).

## Minimum next diagnostic

This is a suggestion only and has not been implemented. First capture one reproduced failing request's exact URL, timestamp, HTTP status, and generic response body, and identify whether the route is public or owner/private.

If the origin's `identity is invalid` response is reproduced, temporarily capture one bounded internal reason category for that request: `invalid_audience`, `invalid_issuer`, `expired`, `invalid_signature`, or `jwks_unavailable`. Continue returning the same generic browser response. Do not log JWTs, cookies, claims, email addresses, configured audience values, or raw exception text.

Cloudflare also documents a scoped identity endpoint in [Extend Cloudflare Access with Workers](https://developers.cloudflare.com/cloudflare-one/tutorials/extend-sso-with-workers/), but it was not needed or called for this audit.

## Follow-up: reload-dependent identity failures

The user subsequently reported that reloading usually resolves the error and authorized deployment, NUC cleanup, and investigation. The NUC's Windows Time service reported no successful synchronization, leap indicator 3, stratum 0, and the local CMOS clock. Seventeen Cloudflare HTTP Date samples had a median server-date minus NUC midpoint of +1.107 seconds; the corresponding synchronized Mac measurement was -0.484 seconds. Accounting for HTTP Date's one-second resolution, these measurements imply the NUC was approximately 1.59 seconds behind.

The origin previously used PyJWT with zero clock tolerance. A newly issued signed assertion can therefore fail its `iat` check until the NUC clock catches up. That mechanism fits the reload-dependent symptom, but remains an inferred cause: no original failing assertion was collected. PyJWT documents this check and its bounded `leeway` option in its [API reference](https://pyjwt.readthedocs.io/en/stable/api.html).

Windows Time stopped during the first resynchronization attempt. Its startup type was changed from Manual to Automatic, the service was started, and `w32tm /resync /rediscover` succeeded. At 2026-09-09T01:22:39Z the service remained Running/Automatic, with leap indicator 0, stratum 5, and a successful sync at 2026-09-09T01:18:45Z. The existing `time.windows.com,0x9` peer was retained. The running quiz release remained healthy. Nonsecret before/after receipts are under `C:\ProgramData\OMSStudyHub-V2\backups\quiz-20260908-921b2ef4`.

The follow-up origin change allows five seconds of JWT clock tolerance using the existing PyJWT decoder. RS256 signature validation, issuer, audience, required claims, and exact owner-email checks remain enabled. The tolerance applies to PyJWT time checks, including expiry. Invalid assertions retain the generic browser 401 response; the server logs only one fixed reason code (`not_yet_valid`, `expired`, `invalid_audience`, `invalid_issuer`, `invalid_signature`, `jwks_unavailable`, or `invalid_assertion`). No raw exceptions, tokens, cookies, claims, emails, or paths are added to this log. There is no automatic page reload or answer-submission retry.

Signed-RS256 regression tests cover small positive clock skew, rejection beyond the tolerance, expiry, incorrect audience/issuer/signature, and JWKS connection failure. Middleware coverage checks the unchanged response and sanitized log. Focused verification passed 21 tests; related public/auth boundary coverage passed 41 tests, with Ruff and MyPy clean. Deployment identity and postflight results belong in the release receipt, rather than being inferred from these offline checks.
