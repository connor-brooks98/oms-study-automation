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
