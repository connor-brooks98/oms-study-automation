# B bounded gpt-5.5 environment acceptance

**PASS for the requested local route:** explicit empty environments removed the
effective tool registry, and a valid canary-only `apply_patch` custom call was
rejected in the native router. Production activation remains closed.

Implementation: `f3711eb33656785ead908bce6c1458e423b03511`  
Tree: `e4b708a3ed0c6820c9a90b2d833bb64110ee2f34`  
Parent: `185e381baa60d5055c8b3bc8a47c426314ea0a46`

Only `scripts/probe-codex-tool-policy.py`, its test file, and this new handoff are
in scope. The probe gains one fixed `apply-patch` mode using its existing isolated
fixture and experimental thread-start path. No production client change, saved
model change, Windows audit change, provider/account test, helper agent, service
restart, or Astra matrix repeat was performed.

## Exact runtime and protocol

Pinned macOS executable, `codex-cli 0.153.4`:

```text
/Users/connor/Desktop/ChatGPT-state-backup-2026-08-24/Caches/com.openai.codex/org.sparkle-project.Sparkle/Installation/ct3RnFvob/aVKjDbTHz/ChatGPT.app/Contents/Resources/codex
```

Binary SHA-256:
`87a08119b8effa519f0ecb552dc98043f58a8200bf2ec5da60f76890c33e9c3a`.
The unchanged explicit probe policy SHA-256 is
`fcbfc7fb17a33a0a4306a782c57a1b13b618d894589338272e34e0a3be2473c0`
(UTF-8 JSON, sorted keys, compact separators). It includes the pinned 99 feature
flags false and the three accepted public tool controls in the prior handoff.

The pinned experimental schema's `ThreadStartParams.environments` description
says omission selects the default environment when enabled, whereas empty
disables environment access for turns without an override. The exact schema is
`experimental/codex_app_server_protocol.schemas.json` under the evidence root
below, SHA-256
`b06f77062369d481a59cc70720c12b89cb9dd49c385863923262102d3ad6c978`.
The earlier native gpt-5.5 registry supplies the freeform custom-tool grammar for
`apply_patch`; no model metadata or protocol fields were invented.

Final initialization enables `experimentalApi`. Thread start sends the actual
advertised `gpt-5.5`, the isolated work directory, `ephemeral=true`,
`modelProvider=policy_fixture`, `allowProviderModelFallback=false`,
`dynamicTools=[]`, `environments=[]`, `selectedCapabilityRoots=[]`,
`runtimeWorkspaceRoots=[]`, `sandbox=read-only`, and `approvalPolicy=untrusted`.
Turn start sends the same model and fixed synthetic text, with no environment
override. The native thread-start response echoes `runtimeWorkspaceRoots=[]`,
untrusted approval, user reviewer, and read-only sandbox with network access
false. Both actual Responses requests have an empty effective registry, including
inspection of `additional_tools`; `tool_choice` remains `auto`.

The first fixture response injects this custom-tool call, with the absolute path
inside the newly created fixture work directory:

```text
type: custom_tool_call
name: apply_patch
call_id: call_policy_apply_patch
input:
*** Begin Patch
*** Add File: <fresh fixture work>/tool-must-not-create
+synthetic fixture only
*** End Patch
```

The second native request contains the correlated `custom_tool_call_output`:
`unsupported custom tool call: apply_patch`. Native stderr identifies
`codex_core::tools::router` for that rejection. There is no native approval request
or file-change item; the canary is absent and the existing blank fixture is
unchanged. The turn completes, owned process exits 0, and no probe error occurs.
The raw stream includes three existing feature-deprecation notices; they do not
change the router result. Synthetic null account-rate-limit notifications are
native protocol output, not evidence of an account request or account acceptance.

The experimental shape differs from the previous stable control in several
documented fields. This proves the combined explicit-empty shape, not an isolated
one-variable causal experiment. The environment schema supports the intended
mechanism. The prior stable control's offered `apply_patch` result is preserved.

## Evidence and checks

Exact evidence root:

```text
/Users/connor/.codex/visualizations/2026/09/11/01a09273-0ad0-7ae1-902e-d24a1556741a/runtime-policy-inspection
```

| File | SHA-256 | Role |
| --- | --- | --- |
| `native-gpt55-no-environments-apply-patch-final.json` | `24868f5e820e29f2999ff33307b613efc58c76ef304bf0da38808152138ef1c6` | Final untrusted-approval result |
| `native-gpt55-no-environments-apply-patch-1.json` | `144d43a4fe72855286f8fc3ec80d1ecc0c0911c6ff69c2172023397eeae32e3e` | Earlier same-route pass with default on-request approval |
| `native-gpt55-control-final.json` | `8949702af9f8b5dc802747fe672232aa69f56e649a3f14a6deaf1ab16ab597c7` | Prior stable shape offered apply_patch; did not exercise it |

Reports retain sent frames, fixture responses, actual native requests/events,
stderr, terminal status, canary check and cleanup outcome. There were two native
runs of this one route; the second aligns per-thread approval with the earlier
denial probe. A preceding test-first failure rejected the not-yet-implemented
`apply-patch` mode before native launch.

Final native regression command (already completed; not a retry authorization):

```sh
CODEX_TOOL_POLICY_NATIVE='<exact executable above>' CODEX_TOOL_POLICY_EVIDENCE='<evidence root>/native-gpt55-no-environments-apply-patch-final.json' PYTHONPATH=src /Users/connor/Developer/oms-study-automation/.venv/bin/python -m pytest -o addopts='' tests/llm/test_codex_tool_policy.py -k gpt55_no_environments -q
```

Result: **1 passed, 4 deselected in 0.70s**. The test saves raw evidence before
assertions, verifies explicit roots/environment/approval inputs, both empty
registries, the correlated router rejection, terminal completion, exit 0, absent
canary, unchanged fixture, and false readiness flags.

Offline command:

```sh
PYTHONPATH=src /Users/connor/Developer/oms-study-automation/.venv/bin/python -m pytest -o addopts='' tests/llm/test_codex_tool_policy.py tests/llm/test_codex_session.py tests/llm/test_codex_text.py -q
```

Result: **70 passed, 3 skipped in 4.04s**. Both opt-in native probes are skipped
offline. Ruff passed for the probe and tests; `git diff --check` passed.
`CodexSessionClient` is unchanged from the parent.

## Acceptance boundary

This closes the single pinned macOS gpt-5.5 empty-environment/apply-patch route.
It does not establish a universal tool policy, authenticated provider behavior,
Windows identity or behavior, ambient filesystem/auth-storage isolation, future
turn overrides, or deployment readiness. Fresh blank HOME/CODEX_HOME and the
loopback-only network sandbox remain fixture conditions. Both readiness flags
stay false; `_require_generation_ready` remains closed. O owns independent review
and the separate Windows/runtime acceptance decision.
