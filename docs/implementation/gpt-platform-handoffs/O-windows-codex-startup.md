# O — pinned Codex startup under LPAC

Source candidate: `0ec321f90d95658aec7e8b4b712e434870339cbd`, tree
`776e5033c1958d2a4ab7cf7bef4ac0730cecd1ab`. Probe SHA256:
`4dbcd2e33311689bf9d8f06c86d76809260b0f4dd32950a4b726408a816ff294`.

## Implemented and tested

The standalone Windows probe adds a fixed `-CodexVersion` mode. It verifies and
copies the pinned Codex 0.153.4 executable, supplies a new empty explicit home
with a separate temporary directory, and launches only `--version`. Only that
new home is writable. Suspended LPAC token checks, both fixed capabilities,
child-process prohibition, detached startup, three inherited file handles and
bounded reaping remain unchanged. The executable hash is checked again after
successful output validation. No auth is copied, no provider is contacted and
no production activation code changes.

The actual Windows version run used task `OMS GPT LPAC Codex Version c7c9e2ca0657`,
profile `oms-lpac-c7c9e2ca06574a798ffc4ae3e9afaa5a`, native child PID1316.
The child exited0 with exact stdout `codex-cli 0.153.4\n`, but stderr reported
failure to canonicalize the explicit private CODEX_HOME with Windows error5.
The strict oracle correctly failed; this is not accepted runtime startup.
There was no timeout or forced termination. Both live-Hub preservation checks
passed; the task is disabled. Outer postflight: `2026-09-13T02:56:38.7110276Z`.

The unchanged default read-boundary mode passed on the same source in a fresh
undebugged task `OMS GPT LPAC Read Regression bf68b23436db`, profile
`oms-lpac-bf68b23436db4bdba0b4d7510e22bc7e`, child PID22268. The child read only
the inside canary, reported outside access denied and exited1 as required;
wrapper and SSH transport exited0. Both preservation checks passed at
`2026-09-13T03:03:44.9816666Z`; task disabled, no timeout or cleanup errors.

Independent source/wrapper review by `/root/review_production_policy` passed.
Actual Windows PowerShell parsed all three scripts and compiled the native probe
in both runs. The standalone change did not rerun the unrelated full core suite;
its earlier results retain their own source bindings.

## Investigation boundaries

Read-only ACL inspection confirmed package Modify rights on the new home and
plain directories. Pinned Codex source reports this error specifically from
home canonicalization. Rust1.95 uses CreateFileW with desired access0 and
GetFinalPathNameByHandleW with DOS volume naming. Firsthand upstream reports
identify DOS-name resolution failures in AppContainer even with broad file ACLs.
These reports motivate the native trace; they do not independently prove this
host's failing call.

Omitting CODEX_HOME is rejected: the pinned Windows dirs dependency resolves
FOLDERID_Profile through SHGetKnownFolderPath, rather than HOME/USERPROFILE.
That shortcut would not prove the actual home stays in the fresh fixture.
No shared ACL, capability expansion, registry change or binary patch was made.

Primary references:

- [Pinned Codex home selection](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/utils/home-dir/src/lib.rs)
- [Rust1.95 Windows filesystem implementation](https://github.com/rust-lang/rust/blob/1.95.0/library/std/src/sys/fs/windows.rs)
- [Microsoft MXC reproduction](https://github.com/microsoft/mxc/issues/694)
- [Microsoft STL reproduction](https://github.com/microsoft/STL/issues/6286)
- [Pinned Windows dirs implementation](https://docs.rs/crate/dirs/6.0.0/source/src/win.rs)

Actual provider acceptance, full restricted runtime acceptance and deployment
remain pending. Generation stays `capability_unverified`. Optional AMBOSS,
vendor exports and real Anki prefix configuration remain separate pending lanes.

## Native trace: instrumentation correction

Task `OMS GPT LPAC Codex Home Trace 98729cffbd39` used the unchanged source
under the pinned portable CDB. API entry/return logging worked for a startup
CreateFileW call, but the debugger stopped at the child's initial
`ntdll!LdrpDoDebuggerBreak` exception before home canonicalization. The existing
`ibp` filter did not resume this child breakpoint. The 60-second watchdog
terminated/reaped the owned debugger tree; no native acceptance result was
produced. This failed diagnostic does not prove the hypothesized API failure.
Both preservation checks passed at `2026-09-13T03:13:05.5161147Z`; no cleanup
errors, surviving diagnostic processes or Hub drift; task disabled. The next
fresh trace adds an explicit breakpoint-exception resume rule, retaining API
breakpoint handlers and all prior restrictions.

The fresh guarded trace `OMS GPT LPAC Codex Home BPE 13f6094b41db` also stopped
before the home lookup: MASM rejected the C-style `&&` operator in its loader
breakpoint guard. The failure and raw transcript are retained. The watchdog
again reaped its owned tree; both preservation checks passed, task disabled,
no cleanup errors. The subsequent trace corrects the guard to documented MASM
`&` on parenthesized comparisons and uses documented `r $t19` assignment. This
is a diagnostic syntax correction, not a Codex or security-policy change.

## Confirmed failing native call

Corrected task `OMS GPT LPAC Codex Home MASM 1c725e6b7d86`, profile
`oms-lpac-1c725e6b7d864e94a7fb5e461ddc39ea`, completed without timeout or forced
termination. Codex PID11628 (`0x2d6c`) exited0 with exact version stdout; its
startup warning still correctly failed the acceptance oracle. Suspended token
verification passed: AppContainer1, LPAC access mask2, both fixed capability
SIDs/attributes, exact fresh profile and session1.

The retained `remote/run/probe.stdout`, lines454–469, proves:

1. CreateFileW opens the explicit home with desired access0, share7,
   OPEN_EXISTING and FILE_FLAG_BACKUP_SEMANTICS; handle `0xf0`, Win32 success.
2. GetFinalPathNameByHandleW receives that handle with flags0 (DOS naming).
3. Within that call, CreateFileW on `\\.\MountPointManager`, desired access0,
   returns INVALID_HANDLE_VALUE with Win32 error5 / NTSTATUS `0xc0000022`.
4. GetFinalPathNameByHandleW then returns0 with error5.

The same sequence repeats for subsequent home lookups and the staged executable.
This establishes a runtime/Windows volume-name compatibility failure; the home
handle itself opens successfully. It does not establish an NT-name fallback or
prove that granting access to one shared object would be sufficient.

Both preservation checks passed at `2026-09-13T03:27:04.7507589Z`: live Hub8268, exact
revision/tree/schema and three healthy workers unchanged; no surviving diagnostic
processes, cleanup errors or registry change; task disabled. No provider request
or credentials were used.

Next bounded implementation: evaluate a compatible runtime canonicalization
change, with a new executable hash and fresh acceptance. Keep explicit private
home and the current read boundary. Do not expand shared object/device ACLs,
drop the home guard, relax LPAC, or enable generation to bypass this result.
A native NT-name fallback is a candidate to verify, not an accepted fix.

Independent final diagnosis/raw-result review: **PASS** by
`/root/review_production_policy`; runtime acceptance remains **FAIL**.

Retained evidence: [`windows-lpac-codex-startup.zip`](../gpt-platform-evidence/windows-lpac-codex-startup.zip),
SHA256 `42635dd74c3efc827caa80d1faee8469458ff1b4aeb633a56e1fda11111e60a1`, 364 manifest files, 551,987 bytes.
ZIP integrity and every manifest byte count/hash verified. Includes the failed
version run, passing read regression, two incomplete debugger runs, and completed
API trace. Public PDB binaries are excluded; their hashes and local retention
paths are recorded. Each consumed task/profile/root remains retained; none is
a retry target.
