# Phase 3 Panopto OAuth Correction

**Date:** 2026-07-23

**Status:** Approved

## Root cause

The original Phase 3 implementation used OAuth client credentials with a plain
Panopto Server Application. That client has no Panopto user identity. Even
when its client ID and secret are valid, it cannot reliably read private
course folders and sessions.

## Corrected authentication

The Hub uses a Panopto **Server-side Web Application** with:

- CORS Origin URL: `https://localhost`
- Redirect URL: `http://127.0.0.1:8765/panopto/oauth/callback`
- Authorization endpoint:
  `/Panopto/oauth2/connect/authorize`
- Requested scopes: `openid api offline_access`
- Token endpoint: `/Panopto/oauth2/connect/token`

The setup page starts a state-protected authorization-code flow. The user
signs in through the normal LMU Panopto SSO page. The callback exchanges the
one-time code using the client ID, client secret, and exact redirect URL.

The client secret and refresh credential are stored only through the existing
Windows Credential Manager abstraction. Access credentials remain in memory.
The Hub refreshes them before expiry and stores a rotated refresh credential
when Panopto returns one. It never requests or stores the user's Panopto
password.

## Safety and lifecycle

- OAuth state is unpredictable, single-use, and expires after ten minutes.
- Callback errors and Panopto response bodies are not logged or rendered.
- A changed client secret clears the old refresh credential.
- Disconnecting removes the refresh credential, pauses Panopto automation, and
  resets acceptance validation.
- Reconnecting also resets acceptance validation so a newly connected user
  must prove access to the configured acceptance session.
- Automatic processing cannot be enabled without the client ID, client secret,
  connected-user refresh credential, OpenAI key, acceptance validation, and
  current approved prompt.
- Immutable transcript originals, cleaned revisions, quarantine behavior,
  checklist transitions, polling rules, and Canvas behavior are unchanged.

## Verification

Automated coverage proves authorization URL construction, redirect and scope
values, state rejection, code exchange, refresh and rotation, in-memory access
reuse, sanitized failures, connect/disconnect dashboard behavior, setup
readiness, and preservation of the existing Phase 1–3 test suite.
