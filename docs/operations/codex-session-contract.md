# Codex subscription session contract — B1/B2

Inspected 2026-09-11. **Offline contract only; Windows, account, image/schema,
tool restrictions and live generation are unverified. Activation is blocked.**

## Observed executable and reproducible schema

The local PATH resolves `codex` to:

```text
/Users/connor/Desktop/ChatGPT-state-backup-2026-08-24/Caches/com.openai.codex/org.sparkle-project.Sparkle/Installation/ct3RnFvob/aVKjDbTHz/ChatGPT.app/Contents/Resources/codex
```

This backup-directory executable is the inspected macOS artifact, not a selected
Windows installation or deployment source. No tokens or account files were read.

| Evidence | Observed value |
| --- | --- |
| `codex --version` | `codex-cli 0.153.4` |
| macOS binary SHA-256 | `87a08119b8effa519f0ecb552dc98043f58a8200bf2ec5da60f76890c33e9c3a` |
| Complete schema bundle SHA-256 | `e8284c5cb8157554a3dd1e035aadbd4325aea501af56887e9c2e12eb1b9b9448` |
| Complete bundle bytes | 683139 |
| v2-only bundle SHA-256 | `d3eace08be5dca386bfd1f1e8df650058b4113f1e10870a284d775d75517576a` |
| Export | 304 JSON files, without `--experimental` |

Commands executed: `codex --version`, `codex app-server --help`,
`codex app-server generate-json-schema --help`, then:

```sh
codex app-server generate-json-schema --out /path/to/isolated-schema-export
PYTHONPATH=src python scripts/probe-codex-session.py \
  --offline --schema /path/to/isolated-schema-export/codex_app_server_protocol.schemas.json \
  --version 'codex-cli 0.153.4'
```

The export path above is an operator-selected empty directory. This is schema
generation, not an app-server session. The offline probe only reads the existing bundle;
omitting all mode flags has the same behavior. It rejects a different version or
bundle hash. `--version` is a supplied inspection receipt, not a runtime check.
No binary is executed on import or in offline mode. `--login` and `--smoke`
are mutually exclusive explicit modes. Both require `--executable`, `--session-home`
and `--work-root`; `--smoke` also requires `--model`. Managed login uses the actual
stdio client and keeps the owned process alive while waiting for completion, then
closes it; timeout/interruption cancels the owned challenge. Smoke is implemented
but fails closed before process launch while universal tool restrictions remain
unverified. No actual login challenge or provider turn was run during B2.

The actual Windows executable path/hash and scheduled-task principal are **not
observed**, so no exact runnable Windows deployment command can honestly be frozen.
The inspected argument vector for future Windows verification is
`[verified_absolute_codex_exe, "app-server", "--listen", "stdio://"]`, launched with
`shell=False`, a private session home, and a per-request staging cwd. O must bind
the real path/hash/identity and restriction policy before activation. Reject drift
rather than assuming the macOS binary hash applies to Windows.

## Raw protocol fields frozen from this export

The full bundle uses `definitions/v2` for v2 payloads and top-level definitions
for initialization and envelopes. Standalone payload files are under `v1/` and
`v2/`. The table records schema facts, not account acceptance. The
[official app-server documentation](https://learn.chatgpt.com/docs/app-server)
describes JSONL over stdio and the initialize/initialized handshake. The exported
binary schema is authoritative for the field spellings below.

| Request or notification | Frozen fields and handling |
| --- | --- |
| `initialize` | Request `{id,method,params}`; `params.clientInfo` requires `name,version`; response requires `codexHome,platformFamily,platformOs,userAgent`. Do not expose `codexHome` in public status. |
| `initialized` | Client notification with `method`, no request id. |
| `account/login/start` | Managed device params `{type:"chatgptDeviceCode"}`; response `type,loginId,verificationUrl,userCode`. Browser params `{type:"chatgpt"}`; response `type,loginId,authUrl`. Never use `apiKey`, `chatgptAuthTokens` or other credential-bearing variants. |
| `account/login/cancel` | Params `loginId`; response `status` is `canceled` or `notFound`. |
| `account/login/completed` | Required `success`; optional `loginId,error`. Challenge creation alone is not connection success. |
| `account/read` | Params may set `refreshToken:false`; response requires `requiresOpenaiAuth`; `account` may be null. Managed account has `type:"chatgpt",email,planType`; do not log email. |
| `model/list` | Params `cursor,limit,includeHidden` optional. Response `data`, optional nullable `nextCursor`. Follow cursors until absent/null. Select the `model` slug, not display name. |
| `account/rateLimits/read` | No required params; response requires `rateLimits`; optional `rateLimitsByLimitId` map. Prefer supplied per-bucket data. |
| `account/rateLimits/updated` | Required `rateLimits`. Windows have required `usedPercent`, nullable/optional `resetsAt,windowDurationMins`. Reset uses Unix seconds; absence remains unknown. |
| `thread/start` | Optional `model,cwd,baseInstructions,developerInstructions,config,ephemeral,approvalPolicy,sandbox`. Response includes `thread.id` plus required `approvalPolicy,approvalsReviewer,cwd,model,modelProvider,sandbox,thread`. Persist thread identity before turn dispatch. |
| `turn/start` | Requires `threadId,input`; `outputSchema` holds JSON Schema. Text input is `{type:"text",text}`; original local image is `{type:"localImage",path}`. Response contains `turn` with `id,items,status`. |
| `turn/started` | Notification `threadId,turn`; persist returned turn identity immediately. |
| `turn/interrupt` | Params `threadId,turnId`; empty response object. Interrupt acknowledgement is not terminal completion. |
| `item/completed` | Requires `threadId,turnId,completedAtMs,item`. Assistant item is `type:"agentMessage",id,text`; `phase` may be `final_answer`, `commentary`, or null/absent. A commentary item is not final output. |
| `turn/completed` | Requires `threadId,turn`; turn requires `id,items,status`; status enum `completed,interrupted,failed,inProgress`. Accept output only after matching successful completion; completion items may be empty. |
| `error` | Requires `threadId,turnId,error,willRetry`; `error.message` required and `codexErrorInfo` optional. Do not surface raw messages or replay ambiguous requests. |
| `item/commandExecution/requestApproval` | Server request id must be answered before execution. Params require `threadId,turnId,itemId,startedAtMs`. Decline response is `{id,result:{decision:"decline"}}`. Existence of this method does not prove every tool is intercepted. |

Model records require `id,model,displayName,description,hidden,isDefault,
defaultReasoningEffort,supportedReasoningEfforts`. `inputModalities` is optional
with schema default `["text","image"]`. `model_ready` intentionally ignores that
default: require explicit list metadata containing `text` and, for images,
`image`. It checks advertised input support only. Enumeration never establishes
synthetic schema/image capability or fills `SessionStatus.image_model_ids`.

Raw error variants include `unauthorized`, `usageLimitExceeded`,
`rateLimitExceeded`, `contextWindowExceeded`, and structured stream failures.
B2 normalizes these to the application allowlist; unknown errors fail unavailable.
No automatic API fallback, reset consumption, account rotation, or generation retry.

## Frozen Python handoff to B2 / O / C

`src/oms_hub/llm/codex_session.py` now contains frozen `SessionRequest`,
`SessionResult`, `SessionLifecycle`, `SessionStatus`, and `LoginChallenge`
dataclasses and the B2 `CodexSessionClient`, `SessionError`, and production event
reducer. O approved two appended default fields: `SessionRequest.image_sha256:
tuple[str, ...] = ()` and `SessionStatus.account_connected: bool = False`.
The account flag is true only after a managed ChatGPT `account/read` response,
independent of model enumeration or generation readiness. The API-key
`LLMProvider` remains untouched.

Implemented B2 consumer signatures:

```python
CodexSessionClient(executable: Path, session_home: Path, work_root: Path)
status() -> SessionStatus
start_login(*, device_code: bool = True) -> LoginChallenge
cancel_login(login_id: str) -> None
generate(request: SessionRequest, *, cancelled: Callable[[], bool],
         on_lifecycle: Callable[[SessionLifecycle], None]) -> SessionResult
cancel(thread_id: str, turn_id: str) -> None
close() -> None
collect_completed_text(events: list[dict[str, object]], *,
                       thread_id: str, turn_id: str) -> str
SessionError(code: str, *, retryable: bool = False, reset_at: str | None = None)
```

`SessionError.code` is one of `auth_required,rate_limited,model_unavailable,
capability_unverified,context_limit,invalid_output,timeout,interrupted,
tool_request_denied,protocol_error`; expose an allowlisted message only.
`reset_at` is ISO-8601 UTC or `None`. Constructor keywords `startup_timeout=30`, `turn_timeout=180`,
`shutdown_timeout=2`, and `binary_sha256=INSPECTED_MACOS_BINARY_SHA256` are available.
All timeout values must be finite and positive. New process startup hashes the
executable against the configured inspection pin before launching it. O owns the single client shared across chat/generation.

Lifecycle callbacks are synchronous durable-write boundaries: `dispatching`
before remote work, `thread_created` before `turn/start`, `turn_started` immediately
after identity arrives, `completed` before output acceptance. Callback failure
aborts/intercepts owned work; no replay after dispatch ambiguity. B2 must correlate
thread/turn ids, reject failed/partial output, distinguish final from commentary,
deny unsolicited requests, and reap only its owned subprocess. See B2's approved
plan for byte/time bounds and cancellation polling.

## Evidence boundaries and remaining activation blockers

The offline probe hashes the complete exported schema, checks required methods,
and JSONL-round-trips 30 synthetic payload fixtures against top-level required,
known and discriminator members. It is deliberately **not** a general JSON Schema
validator or a subprocess/event-reducer test. Nested semantics, response-id
correlation, timeouts, EOF, cancellation, durable callbacks and tool-request
interception are covered separately by B2's production reducer and owned fake
subprocess tests (66 focused checks including the optional local schema export).

The schema exposes read-only sandbox and approval choices, but this inspection
does not establish a universal pre-execution tool denial policy. Read-only does
not prevent shell reads. Approval policy `never` is not a tool-disable switch;
`untrusted` is not proof that every shell/MCP/network/skill/subagent action asks.
The stable `SandboxPolicy.readOnly` schema has only `type,networkAccess`; it does
not itself expose a per-request readable-root allowlist.

Activation therefore requires an independently verified pinned runtime policy or
unconditional pre-execution interception for **every** tool class, plus the
restricted Windows identity and source-root/credential separation specified by
the approved plan. A tool-start event is a failed restriction invariant, not safe
denial. A malicious synthetic source proof must show no tool starts or side
effects. No such proof was run here; no weakened policy is proposed.

Also pending: actual Windows executable/service identity, managed-login challenge
coordinated with O, same-identity session persistence after restart, model/effort
enumeration, bounded synthetic text/image/schema results, and cancellation/limit
proof. No private source or provider turn was sent. B2/B3 offline work may proceed
independently of these live blockers.

## B2 transport and source contract

The client implements initialize, account/status and model pagination, managed
browser/device login and cancellation, thread/turn dispatch, final event reduction,
interruption and close over the actual stdio protocol. One client lock serializes
all turns and account operations. Cancellation polls at 50 ms; a waiting cancelled
caller cannot dispatch. The stdout reader caps a frame at 4 MiB, aggregate stdout
per owned process at 8 MiB and its queue at 128 frames. Stderr is separately drained
into a private 64 KiB tail, never included in public exceptions. A pipe writer
thread allows blocked writes to time out on Windows as well as Unix. Shutdown
terminates/reaps only the owned child, escalating to kill after the configured bound.

Each generation gets a fresh staging directory and owned process cwd. The process
is reaped before its temporary staging directory is removed, including failures.
Only allowlisted platform environment variables plus the dedicated `CODEX_HOME`
are passed; API keys and provider overrides are omitted. Importing or constructing
the client does not create a process. Status can start an account-inspection process
but never starts login or generation. Stopping or losing transport clears its
process-bound pending login challenge; a later explicit login can create a new
challenge. No challenge is restarted automatically. Close is terminal for the client.

For image requests, B3/B4 must supply sanitized PNG bytes already staged under
the client's dedicated work root, with one recorded SHA-256 per `image_paths`
entry in `image_sha256`. The client resolves symlinks, rejects roots outside its
staging area, checks bytes against the record, reuses `sanitize_quiz_image`, requires
its canonical sanitized hash to match, copies only those assets into the new
request directory and verifies the copy. Source library paths cannot be passed
directly. Original caller staging is retained; request copies are temporary.

The reducer excludes other thread/turn output, ignores commentary and deltas,
deduplicates completed items by id, and accepts the last final-answer item only
after matching successful completion. Older phase-less completed assistant items
are used only when there is no explicit final-answer item. Failed/interrupted/EOF
attempts cannot return text. When an output schema was requested, the client also
requires parseable JSON; the caller's Pydantic/schema and source validators remain
authoritative for full output semantics.

Every unsolicited server request is denied before the client accepts more output;
command/file approvals receive `decision:decline`, other requests get a fixed RPC
error. Any tool-start/completion item violates the restriction invariant. This is
interception/detection coverage, **not proof that the runtime asks before every
possible tool**. `_require_generation_ready` therefore rejects all production
generation with `capability_unverified`; there is no configuration boolean to
bypass it. Only tests replace this boundary for their owned synthetic peer, then
exercise the same production client/reader/writer/reducer. Actual runtime policy
and matching model/account capability evidence remain separate activation work.

Lifecycle callbacks receive request ids unchanged (`run_id` or `run_id:batch...`).
Failures after dispatch preserve learned thread/turn ids in terminal callbacks;
early `turn/started` notifications are persisted even before their RPC response.
A persistence exception never returns output and never retries generation. Failed
terminal persistence is left to O's durable recovery state; it cannot be made
successful by swallowing the error or replaying the remote request.
