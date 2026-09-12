# O: independent Windows startup diagnosis

2026-09-12. **The current read-isolation probe selects a payload whose documented
startup requirements conflict with its zero-capability policy.** Microsoft's
LaunchAppContainer README explicitly requires `lpacCom` and `registryRead` for
LPAC `cmd.exe`. Our probe grants neither, then expects cmd's two TYPE commands to
run. This is a concrete defect in the acceptance-test design. It does not name
the particular DLL or initialization operation failing on this host.

## Independent findings

A fresh reviewer (`/root/independent_lpac_diagnosis`) independently read the probe,
contract and both actual native results. No host mutation or execution was
performed by that reviewer. Findings, in order of impact:

1. `scripts/probe-windows-lpac.ps1` creates zero-capability SECURITY_CAPABILITIES,
   selects System32 cmd.exe, and requires a zero-capability child token before
   resume. That combination conflicts with Microsoft's documented cmd example.
   Treat these results as failed startup controls, not a working read oracle.
2. LPAC creation and token verification passed. Both ordinary caller mask3 and
   child mask2, AppContainer1, exact profile SID, zero capabilities and session1
   were observed before resume. Both children exited `0xC0000142`, were reaped,
   and produced no output. Neither result establishes that a canary read occurred.
3. The approved temporary LowBoxConsoleEnabled test did not resolve failure.
   Its original absence was restored and independently verified. No persistent
   setting, missing DLL, bad installation or GPT authentication failure follows
   from the available evidence.

[Microsoft reference, pinned revision](https://raw.githubusercontent.com/microsoft/SandboxSecurityTools/f6263b76dbf77305969aa599ff8a66579aae5a54/LaunchAppContainer/README.md).
The capability requirement is under its Capabilities heading. Exact downloaded
README SHA256: `89df698308a846e0ec3aac67ca39c7c045b1083f7c91220cb0d838fd80e67359`.
The source and retrieval record are retained in the diagnosis archive.

## Read-only inspection of actual pinned binaries

O inspected PE import metadata using Python's standard library. No target binary
was executed or DLL loaded by the inspection; no managed credential was read.

| Binary | SHA256 | Relevant static imports |
| --- | --- | --- |
| System32 cmd.exe | `97ac98b1a92c286054cce55239cfccdfc23a5517bd07fe693072c9ca96c7dabb` | Registry API set, including RegOpenKeyExW and RegQueryValueExW; CRT and console API sets |
| Codex0.153.4 | `444a3f0008050605cae73cd9b7a2dcac61294062dfaab56dd20430fd6498518b` | combase.dll, advapi32.dll, USER32.dll, shell32.dll, plus networking/security libraries |

An import proves a static dependency, not execution of a particular API, a
required capability, or a failing call. In particular, cmd compatibility cannot
establish Codex compatibility; its native dependency set differs.

The existing Windows debugger inventory found no cdb/WinDbg/dumpbin/LLVM readobj/
objdump on PATH and no cdb at the three inspected standard installation paths.
This is a bounded inventory, not proof that no debugger exists anywhere on disk.
No debugger was installed and no process was attached. The read-only health
snapshot at `2026-09-12T16:08:08.2514931Z` still reports the same healthy live Hub
buildf487c622/schema31 and all three workers alive/start count1.

## Consequence for the implementation

The approved B1 contract requires restricted OS identity, staged source roots,
credential separation and pre-execution denial of all model tools. It does not
specify LPAC or a zero-capability count; those are implementation/probe choices.
Adding Microsoft's two capabilities solely to obtain a passing cmd result would
not establish the contract. Their accessible object scope and Codex's actual
startup requirements must be evaluated before any production policy change.

Do not repeat the console setting experiment or grant capabilities as a fallback.
Keep the failed raw evidence and production `capability_unverified` guard.

The next useful diagnostic is a loader transcript from only a fresh owned
synthetic child, with the same token and fixture. Microsoft's initial breakpoint
occurs before DLL initialization; per-target `!gflag +sls` can expose loader
messages. Avoid image-wide IFEO/GFlags settings for shared cmd.exe. No such trace
has been prepared or executed, and an exact debugger executable must first be
available and verified. Do not build a custom debugger merely to avoid that
prerequisite. [Initial breakpoint](https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/initial-breakpoint),
[per-target gflag](https://learn.microsoft.com/en-us/windows-hardware/drivers/debuggercmds/-gflag).

If zero-capability read proof is retained, its eventual payload must have verified
startup requirements compatible with that policy. Even a passing minimal native
canary would leave actual Codex startup, all-tool prevention, account persistence
and provider acceptance separate. This investigation changes the diagnosis and
handoff, not production generation or the approved architecture.
