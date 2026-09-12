# B Windows app-server registry-only scope

**Source-supported yes:** on official release `rust-v0.153.4`, the traced
app-server startup → initialize → ephemeral thread/start with explicit empty
environments → synthetic text turn/start path does not call the normal debug
CLI's Windows setup/ACL reconciliation. This supports a separately reviewed
registry-only diagnostic scope. It is not a native Windows acceptance result.
The source trace below preceded the separate implementation recorded at the end.
No Windows launch was performed in this scope.

All OpenAI source references below are pinned to release commit
`3d2ee51ca2d5db578f328aa75e20aa22c0197c9a`; local copies are retained under the
existing `windows-runner-diagnosis/rust-v0.153.4` evidence directory.

## Traced facts

- Process startup constructs an EnvironmentManager. Its local environment
  constructors allocate state and an asynchronous notification sink; local
  `start_connecting` returns without launching a helper. Remote environments
  from process/config input can connect independently, so inherited environment
  inputs matter even before thread/start. [Environment manager](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/exec-server/src/environment.rs#L268),
  [local process constructor](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/exec-server/src/local_process.rs#L188).
- The Windows sandbox processor constructor stores configuration. Setup runs on
  the explicit setup-start RPC, not its construction or initialize. Do not call
  setup-start or readiness; readiness reads sandbox setup/user records.
  [Windows RPC processor](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/app-server/src/request_processors/windows_sandbox_processor.rs#L10).
- Thread startup defaults environments only when the option is absent. `Some([])`
  stays empty. Environment selection/resolution and snapshots iterate that empty
  list; no selected executor or filesystem is prepared. Session startup carries
  Windows sandbox settings as data. [Thread startup](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/app-server/src/request_processors/thread_processor.rs#L1402),
  [environment resolution](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/environment_selection.rs#L295).
- Existing explicit controls disable shell snapshots, Code Mode prewarm, hooks,
  plugins, skills and MCP tools. Shell snapshot creation and Code Mode prewarm
  are feature-gated. There is no automatic Windows sandbox warmup/setup call in
  the traced initialization and empty-environment session path.
  [Session setup](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/session/session.rs#L1187),
  [prewarm gates](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/session_startup_prewarm.rs#L186).
- Auth initialization still happens, and disabling WebSockets still schedules
  auth prewarm. An explicit auth-free provider resolves unauthenticated headers;
  file-only auth storage confines its auth-file lookup to the chosen CODEX_HOME.
  A process-wide model-refresh worker exists, but remote refresh requires a Codex
  backend or command auth. These facts require clean fixture inputs, not just an
  empty environment-selection array. [Auth-free provider](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/model-provider/src/auth.rs#L195),
  [file storage selection](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/login/src/auth/storage.rs#L517),
  [model refresh gate](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/models-manager/src/manager.rs#L437).

## Required diagnostic preconditions

Use the separately pinned Windows executable with a newly created private
CODEX_HOME and fixture cwd. Use a minimal reviewed process environment with no
inherited credential, workload-identity, remote-executor/noise transport, proxy,
plugin or custom auth settings. Explicitly select file-only auth storage and the
fixed loopback Responses provider with `requires_openai_auth=false`, no auth
command/token/env-key, no WebSockets and no retries. A blank home alone does not
neutralize process configuration. Do not copy or inspect owner auth/setup files.

Keep `windows.sandbox="elevated"` in private diagnostic configuration if asserting
the elevated setting is preserved; do not silently rely on a fresh home's default
Disabled backend. Carry the accepted explicit feature/tool controls and verify
their compatibility with the Windows binary rather than assuming the macOS
feature catalog/hash is portable. Keep analytics off and omit optional app-server
services and unrelated RPCs. These are private fixture settings, not host policy
changes or production-client wiring.

Send only initialize (`experimentalApi=true`), ephemeral thread/start with actual
`gpt-5.5`, `environments=[]`, `selectedCapabilityRoots=[]`,
`runtimeWorkspaceRoots=[]`, `dynamicTools=[]`, and the fixed text turn with no
environment override. Return a synthetic completion from the loopback fixture;
inject **no tool call** in this initial registry-only scope. Capture native request
registries including `additional_tools`, sent frames, raw events, stderr, terminal
state and owned-process cleanup. Any unexpected requested action fails the scope.

## Limits and disposition

This is a focused source trace, not an exhaustive whole-runtime audit or proof
of OS network isolation. It does not establish Windows registry contents, valid
apply_patch router denial, sandbox-account execution, ACL/network enforcement,
provider capabilities or source quality. A later injected-tool test requires its
own reviewed scope; registry absence alone is not universal pre-execution denial.

The existing macOS probe's network-confinement/platform guard remains unchanged.
A Windows port needs separate implementation and review that preserves that
guard; no monkeypatching, platform spoofing, firewall change or native launch is
implied by this finding. The normal debug CLI remains rejected at its known
setup/ACL boundary. Production generation stays closed.


## Separate implementation receipt — 2026-09-12

Implementation commit: `f1f799af7a904752fd2822df83c9f50008519964`.
Tree: `33f23c52528bdd0ef9a9c6be74174fd9d646f292`.
Script SHA-256: `2c27997fba090aa496c37c7096c755b8db4cfeae83fcb8a80f9d4052c4afa339`.

The new `scripts/probe-codex-windows-registry.py` uses only Python stdlib and can
run with `-I -B`, without the repository, PYTHONPATH or OMS dependencies. It rejects
non-Windows execution and any executable other than the separately pinned Windows
SHA-256 `444a3f0008050605cae73cd9b7a2dcac61294062dfaab56dd20430fd6498518b`.
It requires a fresh absolute output directory whose parent already exists.

The proposed O-owned invocation, after independent review/staging, is:

```powershell
& 'C:\Services\oms-study-automation-v2\.venv\Scripts\python.exe' -I -B `
  '<staged>\probe-codex-windows-registry.py' `
  --executable 'C:\Users\conbr\.local\bin\codex.exe' `
  --output-dir '<new diagnostic root>\probe'
```

The probe makes one app-server launch with the reviewed private configuration and
allowlisted environment. It verifies the private CODEX_HOME and returned model,
provider, roots and thread policy; handles completion before the turn/start reply;
denies server action requests; rejects unexpected item types; and returns only a
fixed assistant message from its loopback Responses fixture. A diagnostic pass
requires one request, no offered tools including `additional_tools`, one fixture
response, a successful terminal turn and native exit zero. Every handled failure
returns nonzero. This is a registry diagnostic, not a restriction acceptance gate.

Evidence retained in the output directory includes `prelaunch.json` before Popen,
`native-pid.json` immediately after launch, `sent-frames.jsonl` before sending,
unbuffered bounded `stdout.jsonl`/`stderr.txt` before decoding, fixture request and
response files, and final `result.json` with errors, native PID/exit and cleanup.
The protocol deadline is 40 seconds; child reaping uses bounded 3-second waits,
then terminate/kill escalation for the owned process. Output readers and fixture
socket operations are bounded. The process environment retains only explicit
Windows OS paths and fixture home/temp paths; no parent credential/proxy/executor
settings are copied. No system configuration or owner auth/setup files are changed.

O's independent outer deadline and process-tree postflight remain required for
the native diagnostic. A hard outer kill can prevent final `result.json`; the
incremental files remain available. The script does not claim descendant cleanup,
OS network isolation, sandbox identity, restriction enforcement or provider
acceptance. All those verification flags remain false. No native Windows or
provider run occurred; production generation remains closed.

Offline verification at the implementation commit:

- Test-first missing-implementation check: 5 failed before implementation.
- Focused Windows, Mac policy, session and text tests: **77 passed, 3 skipped**;
  native opt-in and optional schema variables were explicitly unset.
- Ruff format/check passed for both new Python files; `git diff --check` passed.
- Standalone `python -I -B scripts/probe-codex-windows-registry.py --help` passed.
- Mac probe and production `codex_session.py` match the pre-implementation HEAD.

The new tests cover normal/early completion ordering, denied actions, private
configuration and hash rejection without native launch, output limits, effective
`additional_tools` detection, and a real offline loopback fixed-response/extra-
request rejection exchange. Native feature compatibility and registry contents
remain unverified until O's separately owned diagnostic.
