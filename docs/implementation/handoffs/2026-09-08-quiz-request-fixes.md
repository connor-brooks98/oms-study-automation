# Quiz request fixes and live diagnosis — 2026-09-08

## Release state

- Branch: `codex/quiz-request-fixes`.
- Code commit: `525501649fbdf87b39905179457784c186e7bfb9`; tree: `344208f7ebe8597da71a50af5e01065b2d4fb4ad`.
- Base and observed NUC release: `2ec34af8fbc6646138f63d1ce4324036f7805cb8`; tree: `fa10acb4cbaff75f7b4366f264eb2af545cd0d65`.
- Worktree: `/Users/connor/Developer/worktrees/oms-study-automation-quiz-request-fixes`.
- Implemented, tested, independently reviewed, and verified with a synthetic provider request. **Not merged, pushed, or deployed.** No Hub restart or Cloudflare setting change occurred in this investigation.

## 1. Gemini rejects quiz extraction requests

The deployed `quiz_extraction` assignment is Gemini / `gemini-3.8-flash`. The two most recent failed direct-import runs reached the provider request during extraction. This ordinary GenerateContent flow is separate from the Gate 2B File Search rollout.

Controlled source-free probes reproduced HTTP 400 `INVALID_ARGUMENT` with the full `ExtractionPayload` schema. Removing only the schema's `maxItems` keywords changed the result to HTTP 200 with `STOP`. Changing `const`, defaults, string lengths, references, or switching to the legacy response schema field did not independently resolve it. The pre-matching MCQ schema also failed, so removing matching support would not solve the problem.

The fix copies the schema in the shared Gemini adapter and omits array maximum cardinality keywords at schema nodes. The canonical caller schema remains unchanged; local Pydantic validation still enforces all question, choice, and citation bounds. A literal property named `maxItems`, and literal values under `default`, `const`, or `enum`, remain intact. MIME enum `APPLICATION_JSON`, model selection, and unstructured requests remain unchanged. Google documents structured-output complexity limits in its [structured output guidance](https://ai.google.dev/gemini-api/docs/structured-output).

Final proof used the exact fixed adapter, staged as a separate temporary module on the NUC, with deployed dependencies and the existing credential in the signed-in desktop session. One request extracted a synthetic MCQ, a grouped matching question, and an answer for each. HTTP 200; canonical schema validation and source-reference validation both passed. No lecture files were sent, no provider files or stores were created, and no live application module was replaced.

Sanitized final evidence:

```json
{
  "adapter_sha256": "518a3dbb7162a39b2886fdd5aa3212a45d4f84b81e9065091708a7cef358a898",
  "model": "gemini-3.8-flash",
  "request_count": 1,
  "http_status": 200,
  "question_count": 2,
  "question_kinds": ["multiple_choice", "matching"],
  "answer_count": 2,
  "answer_kinds": ["multiple_choice", "matching"],
  "schema_valid": true,
  "source_references_valid": true,
  "status": "passed"
}
```

The complete diagnostic sequence dispatched 12 tiny synthetic GenerateContent requests: seven HTTP 400 responses and five HTTP 200 responses, including the final fixed-adapter proof. Two earlier SSH credential checks failed before any request. The SSH session could not unlock the desktop keyring; the existing desktop session could. This was not evidence of a bad API key.

## 2. Public quiz “Failed to fetch”

Chrome showed the same-origin answer POST redirecting to the Cloudflare Access login domain. The page's `connect-src 'self'` policy blocked that cross-origin redirect, producing the browser's generic fetch error. A top-level authentication refresh followed by one manual retry of Connor's already-selected answer returned HTTP 200, displayed the rationale, and enabled Next. This confirms the live session recovered; it does not mean the new JavaScript is deployed.

The player now shows one recoverable error with a refresh/sign-in link, preserves the answer and progress when storage succeeds, reports storage failure honestly, blocks duplicate submissions and selection changes while a request is pending, and validates successful feedback before marking a question submitted. It never automatically retries an answer POST. Existing CSP, CSRF checks, and Access policies stay in force. Cloudflare documents the possibility of expired Access sessions causing [AJAX requests to fail without an authentication prompt](https://developers.cloudflare.com/cloudflare-one/access-controls/access-settings/session-management/).

## 3. Cloudflare “identity is invalid”

See [the live Access settings audit](2026-09-08-cloudflare-access-audit.md). The owner issuer, audience, and email match the NUC configuration. The NUC can retrieve Cloudflare signing keys. Owner home and the public library loaded during this investigation. The exact intermittent origin error was not reproduced, and the failing URL was requested from Connor. Do not claim an audience mismatch or loosen the owner policy without new evidence.

## Verification and review

- Final combined Python checks: **185 passed** across `tests/llm`, extraction contracts, extraction, and quiz import worker tests.
- Full JavaScript suite: **243 passed**; targeted public quiz tests: **34 passed**.
- Ruff on changed Python files, mypy on the Gemini module, JavaScript syntax check, and `git diff --check` passed.
- Independent Sol review approved both exact file pairs after corrections, with no remaining findings. Frontend review addressed storage failure, malformed success payloads, and safe handling of non-string error details.

| Reviewed file | SHA-256 |
| --- | --- |
| `src/oms_hub/llm/gemini.py` | `518a3dbb7162a39b2886fdd5aa3212a45d4f84b81e9065091708a7cef358a898` |
| `tests/llm/test_gemini.py` | `28764fa0d9b323294b46915b772f7710ee56d998de6bb72eadda33448d8749a8` |
| `src/oms_hub/web/static/public_quiz.js` | `96e9d465fef495931c5b4d53f8a4382d2f85e7616275aaf60b564b17f834caa1` |
| `tests/js/public_quiz.test.js` | `ef73383bc4f2cfec3e431932ab9a2ad5d7185c67a4834675a2a60537166cf129` |

## Handoff and next action

The main orchestrator remains the pinned task **Audit Sol 1–10 handoff state**. SSH alias `nuc` works with strict host-key checking. It should continue its implementation job and keep this ordinary quiz evidence separate from Gate 2B acceptance. The shared `task28a-live-control-acd016d9` runtime was not used or modified by these probes.

To release after the main-Hub restart is authorized: recheck main and NUC HEAD/tree and running work; integrate this exact code commit using the existing guarded release process; preserve the current NUC release as rollback; update the existing `C:\Services\oms-study-automation-v2` deployment and `OMS Study Hub V2` task, then verify one listener on 8765, `/health`, `/health/ready`, and exact build revision/tree. No dependency or database migration change is introduced by this patch. Reopen the quiz after deployment to load the new script. A real quiz-import acceptance check remains separate from the synthetic provider proof.

Retained local diagnostic scripts and sanitized JSON are under `tmp/quiz-request-diagnostic/` in this worktree (untracked); final remote evidence is `C:\Users\conbr\AppData\Local\Temp\oms-quiz-fixed-adapter-probe-20260908.json`. These are evidence, not Gate 2B acceptance or deployment receipts. Credentials and authentication tokens are not included in these artifacts.
