# O: remote-browser ChatGPT connection repair — 2026-09-15

The live Settings button was inert because Chrome rejected `/static/gpt.js` with
`net::ERR_BLOCKED_BY_CLIENT`, although the NUC served the asset successfully.
Rename the shared asset to `study_generation.js` on Settings, lecture, and run pages.
Connect reserves a tab in the current browser during the click, removes its opener,
and navigates to the validated HTTPS device-code URL returned by the Hub. The
visible link and code remain available if a popup is blocked. Failed requests close
only the reserved tab and display the endpoint error. Owner/CSRF checks remain intact.
The route explicitly requests device-code login, avoiding a server-local OAuth callback.

The NUC also lacked both managed executable settings. Read-only verification found
`C:\Users\conbr\.local\bin\codex.exe`, SHA256
`444a3f0008050605cae73cd9b7a2dcac61294062dfaab56dd20430fd6498518b`, matching the
existing Windows account-runtime pin. The release configures these two missing
settings in the preserved environment. Windows generation remains hard gated;
account login is separate from generation acceptance. No provider source turn is
part of this repair.

The upload page now uses the standard wide container. Long filenames occupy a
full row above the type/remove controls, with a stacked workbench on smaller screens.
Provider task-routing clarification is recorded in `O-provider-routing-20260915.md`.

Validation before release: all 299 JavaScript tests passed, including a regression
for click-time client-tab creation, blocked-popup fallback, and failed-login cleanup.
Settings/session tests and upload layout tests passed; one existing opt-in runtime
acceptance test remains skipped. Independent login review found no blocking issues.
Upload layout checked at 320, 390, 768, 1024, and 1440 px without horizontal overflow.

Deployment and actual login outcome are recorded separately under
`/Users/connor/Developer/release-evidence/gpt-login-20260915`; local tests do not
establish Windows account login, provider generation, or deployed acceptance.

Native Windows follow-up: the first real login exposed startup rejection of global
`approval_policy="untrusted"`. A blank-home initialize-only diagnostic captured that
exact stderr. Overriding only this startup value to `on-request` succeeded; the
retained accepted Windows fixture uses the same value. Update the shared startup
configuration accordingly. All feature denials, Windows sandbox requirement,
thread/turn policies, and the hard Windows generation gate remain unchanged.

Repeated Connect follow-up: the pending-login guard returned a protocol error before
issuing any request, causing the browser to close its reserved tab. Retain the full
pending challenge and return it on repeated clicks after an account read consumes
any queued completion/expiry notification. Cancellation and process shutdown still
clear the pending challenge. Expired challenges can be replaced by an explicit click.
Regression tests cover reuse in both login modes, cancellation, and expiry.

Actual account acceptance: entering the exact existing device code in the user's
OpenAI page succeeded; the Hub then reported `account_connected=true`. The earlier
authorization failure's cause is not established. No new challenge or generation
turn was required. Post-update persistence is recorded in the separate receipt at
`/Users/connor/Developer/release-evidence/gpt-pending-login-20260915`.
