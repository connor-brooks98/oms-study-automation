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
messages. Avoid image-wide IFEO/GFlags settings for shared cmd.exe. At this earlier review checkpoint no trace had been executed; the later
portable CDB attempts below supersede that prerequisite status. Do not build a custom debugger merely to avoid that
prerequisite. [Initial breakpoint](https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/initial-breakpoint),
[per-target gflag](https://learn.microsoft.com/en-us/windows-hardware/drivers/debuggercmds/-gflag).

If zero-capability read proof is retained, its eventual payload must have verified
startup requirements compatible with that policy. Even a passing minimal native
canary would leave actual Codex startup, all-tool prevention, account persistence
and provider acceptance separate. This investigation changes the diagnosis and
handoff, not production generation or the approved architecture.

## Implementation and native rerun

Connor requested implementation and a rerun. Source commit `67c58784` now supplies
only the two documented startup capability SIDs with SE_GROUP_ENABLED and checks
the exact two distinct SIDs/attributes before resume. LPAC AccessCheck, exact
profile/session checks, child-process restriction and file-read oracle remain.
The production session policy is unchanged.

Actual native run `OMS GPT LPAC Cmd Caps f66f9c339220` compiled and verified both
capabilities (attributes4/4), but child15324 still exited `0xC0000142` with empty
stdout/stderr. The documented correction is implemented but is insufficient on
this host. No canary read was established. Both preservation checks passed at
`2026-09-12T20:42:25.7444237Z`, with no owned survivors or cleanup errors and the
task disabled. A separate console-setting read confirmed absence at
`20:42:42.6015914Z`; this rerun did not change that setting.

Source SHA256: `e0f721e87a521cedb8100bd682badee0c8220082c372cb02d69e1174a4a05123`.
Native source, wrapper and result reviews passed independently. Raw archive:
`windows-lpac-cmd-capabilities.zip`, SHA256
`7297760e5faac180842ed50acd2a66540841c406c66c9a94ff755e6a83afefde`.
All46 manifest entries and CRCs were verified. The earlier zero-capability results
remain retained; they are not relabeled as acceptance of this corrected token.

## Loader evidence after the implemented correction

Source candidate `67c58784042601d958d1e86879f96c71a8365bda` is unchanged across
these three fresh traces. Portable Microsoft CDB10.0.29617.1000 was extracted
into a new diagnostic directory without installation/Appx registration. All217
remote debugger files were hash checked; CDB SHA256
`5f54abafca3ae5638bbf807d402fabb350a64575c1dfa9fbfc7f5732df5bee67`.
A matching public ntdll PDB was downloaded from Microsoft's symbol server:
SHA256 `879596b54dc0b944e47160dcba01bac56fbc13eaed41fcad50897c6fdd730d1f`.
The executable/tool packages remain retained outside Git; archives record hashes
and public provenance rather than redistributing bulk binaries.

- `windows-lpac-loader-trace`: child20908 reproduced0xc0000142, but !gflag could
  not resolve the PEB without symbols. Both postflights passed.
- `windows-lpac-loader-symbols`: a typed-expression command failed in csc before
  cmd was created. The condensed script stopped before its subsequent guards;
  the watchdog timed out and inner cleanup recorded an owned-process reap error.
  Outer cleanup/preservation passed. Independent reconciliation at21:15:13Z
  confirmed all four exact owned PIDs absent, including PowerShell and conhost.
  This is an instrumentation failure, not a new LPAC child acceptance result.
- `windows-lpac-native-loader`: replaced manual typed-field writes with native
  !gflag +sls, armed only for the new cmd child, and used .catch to prevent command
  errors from stranding a debugger prompt. Removed -netsyms:no, which rejected
  even the explicit local symbol directory on this build; -sins and the cleared
  environment retain a single pinned local path with no configured symbol server.
  Timeout cleanup now stops the owned debugger before debuggees. This last run
  needed no timeout or forced cleanup.

Actual task **OMS GPT LPAC Native Loader 213490c1ead3** ran conbr/session1/Limited.
Raw `remote/run/probe.stdout` lines349–380 show matching ntdll public symbols and
NtGlobalFlag changing0 to2 (`sls`). Lines533–545 identify **KERNELBASE.dll's init
routine failing during DLL_PROCESS_ATTACH**; the failure then propagates through
KERNEL32 and process initialization as0xc0000142. This identifies the DLL/phase,
not the internal API, denied object or underlying cause. Earlier0xc0000135 is a
loader lookup followed by successful mapping and is not the terminal failure.

Child19652 verified exact two capabilities/attributes4, LPAC caller3/child2,
AppContainer1, exact fresh SID and session1. It resumed and was reaped normally,
with empty stdout/stderr and0xc0000142. The read oracle failed. Native PowerShell
parsing and C# compilation passed; this is actual Windows evidence, not a mock.
Both preservation checks passed at **2026-09-12T21:19:05.5724365Z**; task disabled,
no cleanup errors or new/missing monitored processes. The independent console
read at21:19:26.8506816Z confirms LowBoxConsoleEnabled remains absent.

Final trace archive: `windows-lpac-native-loader.zip`, SHA256
`0261961bea28d0854c93352f06b83dc3e4a53897141c38d6eab48ddffa8d6bfe`;
57 manifest files and CRCs verified. Failed instrumentation evidence is retained
in the separate archives listed in the evidence README.

Next bounded diagnostic: matching KERNELBASE symbols and cmd-only initialization
return/stack evidence. Do not infer a permission or capability fix from the DLL
name. Production generation remains capability_unverified; no provider request,
shared registry/ACL/account change, live deployment or Anki mutation occurred.

Independent final evidence review by /root/independent_lpac_diagnosis: PASS.
Verified exact probe/PID, matching symbols and sls transition, two capabilities,
DLL/phase propagation, normal reap, empty outputs and preservation. No actionable
evidence findings; internal failed operation and actual Codex compatibility remain
unproven.

## Detached-start fix and passing native acceptance

2026-09-13. Source commit **844fb9ec37f2add912090c354f8e5d91d558ba61**.

Matched KERNELBASE symbols exposed its actual initialization functions. The first
inspection used an absent symbol name and retained that command failure. A fresh
entry-point/function inspection identified the exact call-return RVAs; thirteen
one-shot breakpoints in a new cmd process then recorded their return values.
NtQuerySystemInformation, CsrClientConnectToServer and critical-section setup
returned success; BaseNls returned AL1. ConsoleShouldAllocateConsole returned AL1,
**ConsoleAllocate returned EAX0xc000049d**, ConsoleInitialize returned AL0 and
its caller returned AL0. The selected post-call status establishes the failing
operation; !gle values were stale even at successful calls and were not treated
as the cause. No symbolic name for0xc000049d is asserted.

The minimal fix changes only the creation flags from0x08080404 to0x0008040c:
replace CREATE_NO_WINDOW with DETACHED_PROCESS. This file-stdio worker needs no
console. LPAC opt-out, exact two enabled capabilities, prohibited descendants,
explicit three inherited file handles, suspended-token checks and file oracle
remain unchanged. [Microsoft creation flags](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags).

Fresh **undebugged** task OMS GPT LPAC Detached 88db6b957b31 ran as conbr/session1/
Limited. Probe SHA256 `c3a6831cba9295e7e233f2e0125d18a62cb5a0e60cfd71fb254978287f69a1bd`. Inner SHA256
`97dcde81423eee427e554c73ae32c4f3ffccb69098f45e87ef72072e52654bd5`;
outer `01accc691b44097ec7b65e05decc251439c5eac7439e2c977b30d2b8756cc120`.
Native PowerShell parsing and C# compilation passed. Child3556 verified exact
LPAC identity and both capability SIDs/attributes4, resumed and was reaped without
timeout or termination. Raw stdout exactly equals the inside canary with CRLF;
raw stderr is exactly `Access is denied.` plus CRLF. Outside canary content is
absent from stdout. Child exit1 satisfies the fixed oracle; wrapper and transport
exited0. Native result Passed=true, Stage=passed.

Both preservation checks passed at **2026-09-13T02:27:39.3408783Z**: live Hub8268,
buildf487c622/treea9f7cc7/schema31, monitored process identities and all three
healthy workers unchanged; diagnostic task disabled and cleanup errors null.
Independent source/flag, wrapper/hash-chain and final raw canary/token/output/
preservation reviews all **PASS**, with no actionable findings.

Archive `windows-lpac-detached-fix.zip`, SHA256 **e8326e70542e26594d320c97e7e3a1e8db0c97973f8b8035100553886366a74c**,
contains248 SHA256/size-verified files and passes all ZIP CRCs. It retains all
three fresh KERNELBASE investigations, the successful detached rerun and symbol
acquisition provenance. Public PDB binaries remain in retained private diagnostic
directories and are listed by hash/size in the archive manifest.

This closes synthetic cmd startup and the tested inside/outside read boundary.
It does not prove arbitrary-path denial, actual Codex startup/transport, account
persistence, all-tool prevention, provider acceptance or deployed behavior.
The next capability is bounded source-free actual Codex startup under the same
required restrictions. No production generation, shared Windows policy/registry/
ACL/account repair, live Hub deployment, provider request or Anki mutation was
performed. Earlier failed receipts remain historical evidence.
