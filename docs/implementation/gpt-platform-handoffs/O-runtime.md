# O runtime continuation — September 11, 2026

Historical receipt: candidate and GPT-5.5 pending statements below are superseded by [O bounded Windows/runtime acceptance](O-windows-runtime.md). Earlier raw failures remain valid evidence.

Base: `159e529487d51c5f038bd492cba2c742105d5965`. Continuation branch: `codex/gpt-runtime-activation`, worktree `/Users/connor/Developer/worktrees/oms-gpt-runtime-activation`.

## Authorization and retained preview

Connor authorized the isolated local preview and actual managed GPT login, then confirmed it worked and asked to continue implementation. The preview remains at `http://127.0.0.1:56460/settings`, PID 79067, frozen base commit above, isolated empty schema-40 database, separate session/work roots, and Anki disabled. Health was rechecked during continuation: HTTP 200, all three workers alive, each start count one and no current error. Original dirty/untracked repository status still matches the O0 manifest.

Actual managed login connected on September 11 ET; safe receipt: `/Users/connor/Library/Application Support/OMSStudyHub/gpt-local-preview-7qai92rp/connection-evidence.json`. Advertised model slugs include `gpt-6-astra`; no selected model was saved by O. This confirms local account connection, not generation or Windows persistence.

No provider generation, private-source test, live NUC change, Anki write, paid fallback, purchase, reset credit, push or main merge was performed or authorized by this continuation.

## O diagnostic correction

Commit `374ea09e`: explicit synthetic smoke now reserves a unique private evidence directory, writes immutable request metadata before calling generate, and persists each lifecycle record synchronously before dispatch can proceed. Terminal raw text is saved and hashed before JSON parsing. Invalid JSON becomes `invalid_output`; numeric `1` no longer passes as boolean `true`. Failure returns retained evidence location and never resubmits. The existing managed-login path and unconditional capability gate are unchanged.

Validation: `PYTHONPATH=src python -m pytest -o addopts='' -q tests/llm/test_codex_probe_evidence.py tests/llm/test_codex_session.py` — **69 passed, 1 optional schema skip**. Focused Ruff and `MYPYPATH=src mypy scripts/probe-codex-session.py` passed. Independent reviewer `/root/review_o1_schema` reproduced the 69 checks and reported **PASS**, no actionable findings.

## B native policy proof

B uses the exact pinned macOS executable (version `0.153.4`, SHA256 `87a08119b8effa519f0ecb552dc98043f58a8200bf2ec5da60f76890c33e9c3a`) with fresh blank HOME/CODEX_HOME, a loopback-only synthetic Responses endpoint and native macOS network confinement. No account session is read or used.

Initial native evidence showed feature flags alone leave the skills namespace and request_user_input registered. A fixed skills.list call executed without an app-server tool event. The [OpenAI configuration resolver](https://github.com/openai/codex/blob/main/codex-rs/core/src/config/mod.rs) and [public TOML types](https://github.com/openai/codex/blob/main/codex-rs/config/src/config_toml.rs) identified additional public controls: `tools.experimental_request_user_input.enabled=false`, `orchestrator.skills.enabled=false`, and `orchestrator.mcp.enabled=false`. The pin accepts these keys; the fixture then advertises an empty tool list and rejects injected skills.list with `unsupported call: skillslist`.

A subsequent stable production-shaped request with the advertised `gpt-6-astra` slug revealed nine tools in `input.additional_tools`: functions.exec, functions.wait, functions.request_user_input_async and six collaboration tools (followup_task, interrupt_agent, list_agents, send_message, spawn_agent, wait_agent). Its registry is not empty, and ordinary feature flags do not disable all model-driven registrations. Ten ordinary injected calls returned router-level unsupported errors; a valid custom `functions.exec` call returned `code-mode host is disabled`. Terminal status was completed, process exit zero, the fixture remained unchanged and no canary was created. This distinguishes ordinary tool dispatch denial from the registered but disabled Code Mode host. It does not prove the remaining model-driven agent and user-input routes are blocked. It is not a real provider/model turn.

Additional valid calls then disproved universal tool prevention: `functions.request_user_input_async` returned `{"accepted":true}` and `collaboration.list_agents` returned the fixture root agent. `functions.wait` returned `code-mode host is disabled`. No helper was spawned and no valid followup/send/interrupt/wait-agent call was attempted. These latter routes remain unaccepted.

O and the independent reviewer therefore rejected production wiring of this partial policy. Keep its exact settings in the diagnostic only; changing 99 unrelated flags (including inverse host-skill discovery and auth-storage controls) without satisfying the required boundary would add risk. The application's runtime behavior and closed generation gate remain unchanged. The single advertised `gpt-5.5` control case had no Astra helper tools but still advertised `apply_patch`; that tool was not invoked, so universal denial is unaccepted there too. Its 14 fixed calls were unsupported, terminal status completed, and the fixture was unchanged. No further model matrix is planned in this continuation. The completed [B handoff](B-runtime-policy.md) binds implementation `ff0f0e9477021ea518a61e0536ba64515db0ed76` and receipt `185e381baa60d5055c8b3bc8a47c426314ea0a46`; O integrated both without conflicts.


## Final local candidate

Code candidate: `b556bd951201db03e07c90569b2866ca9379a33a`, tree `ffae60116f6dbcb308cb3d0bbe00286937475aa7`. O's final typing-only commit adds annotations and pipe invariants to the standalone probe; it changes no policy, native fixture payload or assessment. Independent review passed that delta and the exact B implementation/receipt, including all evidence and schema hashes. No native experiment was rerun for the typing change.

Combined final offline suite: **74 passed, 2 skipped** (session diagnostic, native-policy diagnostic, existing session client and Codex text tests). Both scripts passed Ruff and mypy. Exact log: `/Users/connor/.codex/visualizations/2026/09/11/01a09271-e627-7712-a2fc-ff675db23a8b/runtime-continuation/pytest-final.log`.

Application source, runtime configuration and JavaScript are byte-identical to the accepted base `159e529487d51c5f038bd492cba2c742105d5965`. Its full **3,821 Python / 284 JavaScript** acceptance remains base evidence; this continuation ran the scoped checks above. Existing base archives are retained and were not rebuilt or relabeled as this diagnostic candidate. B and the reviewer are idle. The local preview is still on the unchanged base; no restart is part of this continuation.

Next required work is a reviewed native mechanism that blocks every model-driven tool route, followed by separately scoped Windows identity and provider acceptance. AMBOSS entitlement/reply and exact vendor samples remain optional external pending capabilities. No fresh login or password input is needed for the working local preview.
