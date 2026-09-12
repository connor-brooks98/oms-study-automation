# B runtime policy handoff

**FAIL: universal pre-execution tool denial. Activation remains blocked.**

Implementation commit: `ff0f0e9477021ea518a61e0536ba64515db0ed76`  
Implementation tree: `0738d73c820ff0f80034ae82069e2396e7ca9f98`  
Parent: `b680b22c2a5750a9790b91148c014a4d05ee8077`

Scope is the standalone pinned native probe, counterexample tests, and runtime
contract documentation. `src/oms_hub/llm/codex_session.py` is unchanged from the
parent; no partial-policy helper or launch wiring enters production. Generation
still fails closed with `capability_unverified` before session/work creation.
O's existing session-probe evidence correction is separate and untouched.

## Pinned runtime and policy

Executable (macOS only, `codex-cli 0.153.4`):

```text
/Users/connor/Desktop/ChatGPT-state-backup-2026-08-24/Caches/com.openai.codex/org.sparkle-project.Sparkle/Installation/ct3RnFvob/aVKjDbTHz/ChatGPT.app/Contents/Resources/codex
```

Binary SHA-256:
`87a08119b8effa519f0ecb552dc98043f58a8200bf2ec5da60f76890c33e9c3a`.
The stable combined schema SHA-256 is
`e8284c5cb8157554a3dd1e035aadbd4325aea501af56887e9c2e12eb1b9b9448`;
experimental combined schema SHA-256 is
`b06f77062369d481a59cc70720c12b89cb9dd49c385863923262102d3ad6c978`.
Schemas expose no established universal tool-denial contract.

The probe verifies the exact binary hash and all 99 non-removed feature names.
Its partial policy sets those features false, disables web search and default
apps, and uses the accepted public controls:

```text
tools.experimental_request_user_input.enabled=false
orchestrator.skills.enabled=false
orchestrator.mcp.enabled=false
```

Canonical policy SHA-256, using UTF-8 JSON with sorted keys and separators
`(',', ':')`:
`fcbfc7fb17a33a0a4306a782c57a1b13b618d894589338272e34e0a3be2473c0`.
This is an experiment configuration, not an accepted security policy.

Each final run used a fresh HOME/CODEX_HOME/work directory, read-only thread
sandbox, per-thread `approvalPolicy=untrusted`, `approvalsReviewer=user`, stable
initialize/thread-start shape, and a fixed auth-free loopback Responses fixture.
The child process network sandbox allowed only that fixture port. No real
provider request, authenticated account session, worker creation, or production
service change was performed. Raw sent frames, fixture responses, native requests,
native events, terminal state and cleanup results are retained in final reports.

## Native results

Under the feature-only baseline, `skills.list` executed even with per-thread
untrusted approval; its output was an empty skills list without a tool item event
or approval request. Adding the three public controls removed those baseline
tools for `fixture-model` and made its skills-list call unsupported. This fallback
result does not transfer to advertised model slugs.

For actual advertised `gpt-6-astra`, both native requests offered all nine tools
through `input[type=additional_tools]`, with `tool_choice=auto`:

- `functions.exec`, `functions.wait`, `functions.request_user_input_async`
- `collaboration.followup_task`, `collaboration.interrupt_agent`,
  `collaboration.list_agents`, `collaboration.send_message`,
  `collaboration.spawn_agent`, `collaboration.wait_agent`

The 14 fixed fixture calls had correlated outputs:

| Call IDs | Calls | Result |
| --- | --- | --- |
| 0–9 | skills list/read, ordinary request-user-input, exec-command, shell, read-file, nonexistent MCP, function-shaped web-search, view-image, unnamespaced spawn-agent | Unsupported calls |
| 10 | Valid `functions.wait`, nonexistent fixture cell | `code-mode host is disabled` |
| 11 | Valid `functions.request_user_input_async`, synthetic question | `{"accepted":true}` |
| 12 | Valid `collaboration.list_agents` | Isolated `/root` agent returned |
| 13 | Valid custom-tool `functions.exec`, `text("POLICY_EXECUTED")` | `code-mode host is disabled` |

Calls 11 and 12 refute universal pre-execution denial. Valid namespaced
spawn/followup/send/interrupt/wait-agent routes were not exercised; unsupported
unnamespaced spawn is not evidence about the namespaced handler. The function-
shaped web-search call is not hosted-web execution proof. Initial wrong-shaped
exec payload failure is also not denial proof; the final report uses the proper
custom-tool payload.

The one authorized `gpt-5.5` control offered only `apply_patch` and rejected all
14 fixture calls. `apply_patch` was not exercised. This does not establish
universal denial, select a replacement model, or authorize additional probing.

Both final native runs: two fixture requests, no probe error, terminal
`completed`, process exit 0, no canary created, fixture unchanged.
`restrictions_verified=false` and `provider_verified=false` in both reports.
Successful experiment completion is separate from the failed restriction gate.

## Evidence inventory

All filenames below are relative to this exact local evidence root:

```text
/Users/connor/.codex/visualizations/2026/09/11/01a09273-0ad0-7ae1-902e-d24a1556741a/runtime-policy-inspection
```

| File | SHA-256 |
| --- | --- |
| `native-astra-counterexample-final.json` | `f16326bb4d540caad209fb4876c386ad434d32fc1dfc832cce934ba83e0a4bb6` |
| `native-gpt55-control-final.json` | `8949702af9f8b5dc802747fe672232aa69f56e649a3f14a6deaf1ab16ab597c7` |
| `native-skills-list-untrusted-1.json` | `e29ef713c2e4049be218bd3b9b98e3823a9e0c37cce1d903fea7a2ad06d08126` |
| `native-explicit-registry-1.json` | `9c0eae6a4458bfb0e49ce067874bd65976e6568357b162ec01184a91c7d9fe10` |
| `native-explicit-skills-list-1.json` | `604b6be65e610d04e09fab869be8a2361198c138b87e0c6036af24ecf26a0c3a` |

The combined schemas are `stable/codex_app_server_protocol.schemas.json` and
`experimental/codex_app_server_protocol.schemas.json` under the same root.
Earlier failed experiments and assertions remain there for provenance; the two
`final` reports supersede earlier registry summaries. In particular, the first
summary that counted only exec missed eight additional tools; the final parser,
native regression, and handoff include all nine.

## Validation

Final focused offline command from the implementation worktree:

```sh
PYTHONPATH=src /Users/connor/Developer/oms-study-automation/.venv/bin/python -m pytest -o addopts='' tests/llm/test_codex_tool_policy.py tests/llm/test_codex_session.py tests/llm/test_codex_text.py -q
```

Result: **70 passed, 2 skipped in 4.14s**. The explicit native test was skipped in
that offline command. Ruff for the new probe and tests passed; `git diff --check`
passed. O reports an independent draft review with no actionable findings and
**68 passed, 2 skipped** for its narrower probe/client selection.

Final authorized Astra native regression (already completed; no retry implied):

```sh
CODEX_TOOL_POLICY_NATIVE='<exact executable above>' CODEX_TOOL_POLICY_EVIDENCE='<evidence root>/native-astra-counterexample-final.json' PYTHONPATH=src /Users/connor/Developer/oms-study-automation/.venv/bin/python -m pytest -o addopts='' tests/llm/test_codex_tool_policy.py -k pinned_native -q
```

Result: **1 passed, 3 deselected in 0.71s**. This regression asserts the remaining
handler execution and false readiness flags, not universal denial. The gpt-5.5
control completed separately through the same probe with `--mode denial-matrix
--explicit-tool-controls --stable-thread --model gpt-5.5`; its exact config and
sent protocol frames are in the final raw report.

## Remaining boundaries

CLI TOML `approval_policy=untrusted` was rejected as obsolete; production's
per-thread `approvalPolicy=untrusted` was accepted and echoed. The internal name
`experimental_request_user_input_enabled` was an unknown TOML field; the nested
public name above is accepted. Neither failed configuration proves a missing
capability. Hooks were not accepted as a universal guard.

Blanket false includes inverse `skip_host_skill_discovery=false` and
`secret_auth_storage=false`. The probe's blank HOME differs from production's
omitted HOME. Ambient filesystem discovery and authenticated storage identity
are not proven safe by a network-only sandbox. Windows runtime identity, real
provider/model text/image/schema capabilities, source quality and deployment
acceptance remain pending. No package installation, saved model switch, live
Anki action, service restart, activation or further probe is part of this handoff.
