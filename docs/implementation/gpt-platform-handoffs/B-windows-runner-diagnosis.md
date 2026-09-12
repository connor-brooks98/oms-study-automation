# B read-only Windows runner diagnosis

**The help invocation was parsed as a sandboxed command. The runner then failed
before receiving that command. The cause of its missing pipe connection remains
unknown; no retry or Windows change was performed by B.**

## Findings

The official `rust-v0.153.4` tag resolves through annotated tag
`042fb41b7c813ac7999105e886b2b7aa715b5081` to commit
`3d2ee51ca2d5db578f328aa75e20aa22c0197c9a`. Its CLI defines
`Sandbox(HostSandboxArgs)` and selects `WindowsCommand` at compile time on Windows.
There is no `windows` platform subcommand. `WindowsCommand.command` consumes
trailing arguments, so `sandbox windows --help` passes `windows` and `--help` as
the target command/argument. The release's own help test uses `sandbox --help`.
See [CLI dispatch and help test](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/cli/src/main.rs#L178)
and [trailing command parser](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/cli/src/lib.rs#L150).

The exact timeout text originates in the elevated runner transport. Its order is:
create two named pipes; resolve runner executable; call `CreateProcessWithLogonW`;
connect pipe-in with a 15-second limit; connect pipe-out; send the spawn request;
wait for spawn-ready. This timeout places failure at the first pipe connection,
after process creation returned success and before the command request was sent.
The source attempts to terminate the runner and close handles on this failure;
it does not establish that cleanup succeeded on this host. This timeout is not
classified as a refreshable credential error by the release's retry predicate.
See [runner transport](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/windows-sandbox-rs/src/elevated/runner_client.rs#L200).

The parent waits through `ConnectNamedPipe`; the runner should open pipe-in for
reading, then pipe-out for writing, before reading the spawn request. The message
does not identify whether the helper exited during loading/startup, was delayed,
or could not access its pipe. It is not an execution-policy denial of `windows`,
a target-command failure, or a provider result. See [pipe implementation](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/windows-sandbox-rs/src/elevated/runner_pipe.rs#L106)
and [runner entrypoint](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/windows-sandbox-rs/src/bin/command_runner/win.rs).

These are version-matched official source findings consistent with the exact
recorded error, not a reproducible-build attestation for the installed binary.
The administrator token and enabled sandbox accounts in O's snapshot do not prove
runner startup. The separate MSIX access denial is not evidence for this standalone
runner's cause. The post-attempt snapshot shows two older `codex.exe` processes;
neither is identified as the failed command runner. Hub health, listener PID 8268,
and all three workers remained healthy in that snapshot.

## Proposed next bounded work

For CLI metadata only, the corrected PowerShell invocation is:

```powershell
& 'C:\Users\conbr\.local\bin\codex.exe' sandbox --help
```

This is a reviewed proposal, not executed here. It proves help parsing only.

Before another sandbox launch, inspect only runtime metadata for the actual
resolved `codex-command-runner*.exe`: path, length, SHA-256, signature, timestamps,
and matching process creation/parent/owner/session metadata. The release prefers
a materialized helper under CODEX_HOME's `.sandbox-bin`, with a bundled/sibling
fallback. The main executable hash alone does not pin the helper. Do not inspect
credential contents, reset accounts, change ACLs, or bypass MSIX restrictions.
See [helper resolution](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/windows-sandbox-rs/src/helper_materialization.rs#L62).

If O subsequently authorizes a runner launch after resolving that metadata, the
minimal synthetic command form is:

```powershell
& 'C:\Users\conbr\.local\bin\codex.exe' sandbox -P ':read-only' -- 'C:\Windows\System32\cmd.exe' /d /c 'echo CODEX_SANDBOX_FIXTURE_OK'
```

Run only from an approved isolated fixture directory with bounded outer timeout,
raw output/exit capture and owned-process postflight. The builtin profile spelling
is confirmed in [release models](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/protocol/src/models.rs#L405).
The absolute command and `/d` avoid PATH selection and cmd AutoRun. Success means
only a runner/child round-trip. It does not prove write or network denial.

That command is **not currently a no-mutation acceptance operation**: sandbox
startup can materialize helpers and prepare/reconcile sandbox state before IPC.
A blank CODEX_HOME is not a demonstrated workaround for existing setup. Therefore
no further runner acceptance invocation is feasible under the present no-launch,
no-setup/account/ACL-change boundary. Keep that gate closed rather than silently
selecting the legacy backend or changing sandbox configuration.

## Evidence receipt

Standalone binary identity supplied by O:
`C:\Users\conbr\.local\bin\codex.exe`, version `0.153.4`, SHA-256
`444a3f0008050605cae73cd9b7a2dcac61294062dfaab56dd20430fd6498518b`.

Raw evidence root:
`/Users/connor/.codex/visualizations/2026/09/11/01a09271-e627-7712-a2fc-ff675db23a8b/windows-acceptance`.

| File | SHA-256 |
| --- | --- |
| `windows-sandbox-help.stdout` (empty) | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `windows-sandbox-help.stderr` | `125d612a412457bff646d20d9518cf2181b78e3f947257cd9a3959b5071ad8c4` |
| `initial-preflight.stdout` | `f178d0b48ac61293c6f6a457fff147d595a853b5bad65a1f0fb11221df4f01b2` |
| `runtime-inventory-2.stdout` | `a1dc81db6eed3c7197b30e018273ef42e5fde3d67e9a763b155449319444d90e` |
| `post-sandbox-attempt.stdout` | `5908515213d8a32001b651f3da8d4c07df3bdd026ba2ba0dbf3f42cf549175c4` |

Official source snapshots and the tag/source manifest are retained at
`/Users/connor/.codex/visualizations/2026/09/11/01a09273-0ad0-7ae1-902e-d24a1556741a/windows-runner-diagnosis`.
Use the `rust-v0.153.4/` snapshots; initial main-branch inspection is superseded.
B performed local artifact reads and public source retrieval only. No native
launch, remote executable invocation, configuration/account/provider operation,
or runtime test was performed. Production generation remains closed.

## Pre-pipe startup follow-up

The version-matched helper wrapper calls `win::main()` directly. Before opening
pipe-in, that function only parses the two pipe arguments and checks that both
exist. It does not initialize persistent logging, read a credential file, prepare
sandbox state, select the child desktop, or create the child's restricted token.
`open_pipe` performs one `CreateFileW` with `FILE_GENERIC_READ`, zero share mode,
`OPEN_EXISTING`, and no retry. A failure returns its Win32 code in a Rust main
error; it cannot yet send an IPC error frame. The parent does not configure
`STARTF_USESTDHANDLES` or a runner stderr capture pipe. Therefore the recorded
parent timeout can hide an early runner error, and missing helper log lines do
not discriminate loader failure from pipe access failure. [Helper entrypoint](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/windows-sandbox-rs/src/bin/command_runner/main.rs),
[first pipe open](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/windows-sandbox-rs/src/bin/command_runner/win.rs#L553).

The initial helper CWD is the parent's resolved sandbox command CWD, passed
directly to `CreateProcessWithLogonW`; it is not automatically the helper directory.
The launch passes no profile-loading flag, a null environment pointer, and a
zero-initialized `STARTUPINFOW` apart from its size. Child CWD fallback/junction
logic runs only after the spawn request arrives. The original CWD is not present
in O's retained snapshots, so its actual value remains unverified. [Launch parameters](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/windows-sandbox-rs/src/elevated/runner_client.rs#L340).

Named-pipe access grants generic-all only to the selected sandbox account SID:
`D:(A;;GA;;;<sandbox SID>)`. Pipe-in is parent-outbound/runner-read; pipe-out is
parent-inbound/runner-write. The parent also checks the connected client PID.
The administrator token does not substitute for that sandbox SID, and filesystem
ACL metadata does not prove the transient pipe DACL. [Pipe security](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/windows-sandbox-rs/src/elevated/runner_pipe.rs#L55).

For x64 MSVC the release build configuration requests `+crt-static`; installing a
VC++ redistributable is not justified by this source evidence. The package build
script embeds its explicit manifest only in the setup helper. Actual PE machine,
imported DLL/API dependencies and loader events for the resolved runner remain
authoritative; Cargo dependency names do not establish a DLL inventory. No
Node/Python/provider initialization occurs on this pre-pipe path. [Build configuration](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/.cargo/config.toml#L1),
[setup-only manifest](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/windows-sandbox-rs/build.rs).

A future **direct, no-argument** invocation of the already identified and hashed
runner would avoid the parent setup/credential path:

```powershell
& $verifiedRunnerPath 2>&1
$LASTEXITCODE
```

Expected result is exit 1 with `Error: runner: no pipe-in provided`. There is no
helper help/version mode. This proposal has not been executed: it would prove PE
loading and reaching main under the caller (`conbr`) only. It cannot establish
sandbox-account loading, pipe ACL correctness, or a successful runner handshake.
Do not supply live pipe names, change identity, or invoke the setup helper.

The safe existing parent-log subset is helper source/destination selection only.
When O confirms the default CODEX_HOME, its path is
`C:\Users\conbr\.codex\.sandbox\sandbox.2026-09-12.log`; otherwise use the same
relative path under the verified CODEX_HOME. Filenames use UTC dates, while line
timestamps use local time. Avoid whole-log output and command previews. [Log naming](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/windows-sandbox-rs/src/logging.rs#L34).

```powershell
Get-Content -LiteralPath $verifiedLogPath -Tail 400 |
  Where-Object { $_ -match '^\[[^]]+\] helper (copy: (validating|reused|recopied) command-runner source=|launch resolution: using copied command-runner path )' } |
  Select-Object -Last 12
```

These allowlisted lines identify the helper actually chosen; they do not explain
its loader or pipe failure. No existing pre-pipe runner log or standalone command
can distinguish both causes under the sandbox account without a new launch or
instrumentation. O's bounded crash/loader-event and PE/ACL metadata checks are the
next discriminator. New source files are retained alongside the prior release
snapshots. This follow-up performs no Windows launch or remote mutation.
