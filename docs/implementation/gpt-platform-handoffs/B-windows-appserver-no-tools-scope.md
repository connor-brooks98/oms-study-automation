# B Windows app-server registry-only scope

**Source-supported yes:** on official release `rust-v0.153.4`, the traced
app-server startup → initialize → ephemeral thread/start with explicit empty
environments → synthetic text turn/start path does not call the normal debug
CLI's Windows setup/ACL reconciliation. This supports a separately reviewed
registry-only diagnostic scope. It is not a native Windows acceptance result.
No implementation or Windows launch was performed here.

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
