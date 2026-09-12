# O: bounded Windows LPAC investigation

2026-09-12. **Runtime-policy fixes are reviewed and locally tested. Actual Windows
LPAC identity verification now passes; child initialization still fails with
`0xC0000142`, before either synthetic read. Read-isolation acceptance has not passed.**
Production generation remains `capability_unverified`.

## Implemented corrections

- Query scalar token values using exact four-byte buffers. The original zero-size
  query for class20 returned error24 and size4, not the assumed error122.
- Decode the PowerShell5 JSON process ledger before wrapping it as an array;
  otherwise cleanup receives array-valued PIDs. Retain PID/name/start-time checks.
- Capture Win32 errors immediately and retain the numeric native error code.
- Supply validated LOCALAPPDATA in the explicit child environment. This single
  change resolved CreateProcessW error203: the next native run created the child.
- Replace unsupported token class46 with Chromium's functional LPAC AccessCheck.
  The host returned Win32 error87 and native `STATUS_INVALID_INFO_CLASS` for46.
  An in-memory descriptor distinguishes ordinary caller mask3 from LPAC mask2.
  It changes no filesystem ACL and does not impersonate a thread.

Current probe source SHA256:
`e04780505e9e8ad42b7eb52792cfd89b9b3fac09653719f9977f8516154fe4d1`.
Independent source and wrapper review passed. Native PowerShell parsing and C#
compilation passed. Exact sources and raw results are retained in the archives.

## Fixed scope and safeguards

The standalone probe uses a fresh retained AppContainer profile and synthetic
fixture under a new acceptance directory. Only the new fixture ACLs change:
package traverse on its root and read/execute on its inside directory. The sibling
outside canary receives no package grant. No shared Windows directory/account ACL,
firewall, loopback exemption, provider credential or live source is changed.

The sole native child is pinned System32 cmd.exe, with fixed command:

```text
/d /v:off /c "type inside.txt & type ..\outside.txt"
```

Launch attributes require zero capabilities, ALL_APPLICATION_PACKAGES opt-out,
three explicit standard-stream handles, and prohibited descendant processes.
The explicit environment contains COMSPEC, LOCALAPPDATA, SystemRoot, TEMP, TMP
and WINDIR. Before resuming the suspended child, require AppContainer1, exact
new profile SID, zero capabilities, session1, caller AccessCheck3 and child2.
Any mismatch fails closed. The child has a15-second wait and5-second owned-handle
termination/reap bound. Profiles, fixtures and disabled tasks remain retained.

The intended read oracle requires inside-only stdout, exact English access-denied
stderr, cmd exit1, and unchanged canaries. A startup failure cannot satisfy it.
The wrapper independently checks conbr/session1/Limited and the live Hub before
and after, and bounds cleanup to recorded process identities.

## Native results, retained separately

| Archive | Actual result |
| --- | --- |
| windows-lpac-acceptance | Caller scalar-token sizing failed before profile/child. Outer cleanup also hit the JSON-ledger defect. Independent reconciliation found all owned processes gone; Hub preserved. |
| windows-lpac-retest | Token/ledger corrections passed; profile created; CreateProcessW failed before child. Both postflights passed. |
| windows-lpac-create-error | Retained CreateProcessW error203; no child. Both postflights passed. |
| windows-lpac-localappdata | Environment correction created child17664. Unsupported token46 check stopped it before resume; owned child terminated/reaped. Both postflights passed. |
| windows-lpac-accesscheck | Caller3/child2, AppContainer1, exact SID, zero capabilities and session1 passed. Child3424 resumed and exited3221225794 (`0xC0000142`) with empty stdout/stderr. Read oracle failed. Both postflights passed. |

Previous task: `OMS GPT LPAC AccessCheck 3092ba4a06da`, disabled, last result1.
Profile: `oms-lpac-3092ba4a06da4dcabb6e9e3943257907`.
Remote root: `C:/Users/conbr/AppData/Local/OMSStudyHub/acceptance/lpac-3092ba4a06da`.
Native run: `2026-09-12T14:27:10.9826022Z` to `14:27:11.0368589Z`.
No timeout or kill was needed; child reaped. Previous outer postflight
`2026-09-12T14:27:17.0931795Z`: no new/missing monitored processes, cleanup error
null, task disabled, same healthy Hub listener PID8268/buildf487c622/schema31 and
three workers alive with start count1. A narrow read-only System/Application event
query for14:26:50–14:27:30Z found no matching events. It does not identify the DLL.

Previous archive manifest:46 files, SHA256
`9701b1ba9b662ee852dc6f22f964b6a3f2e1466396bf9bcef6114dd03b89d612`.
[Archive hashes and retained failures](../gpt-platform-evidence/README.md).
Original raw evidence remains under O's visualization directory.

## Remaining boundary

`0xC0000142` establishes failed DLL initialization, not which DLL or missing access.
Do not infer successful reads from the valid LPAC token, weaken the token, or alter
shared ACLs to obtain a pass. Pinned Codex startup, all-tool prevention, managed
credential separation, Windows login persistence and provider/model text/image
acceptance remain separate gates. This standalone probe is not a production
launcher; the production readiness guard remains closed.

## Primary implementation sources

- [Microsoft AppContainer launch and LOCALAPPDATA rerouting](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer).
- [Chromium CheckLpacToken](https://chromium.googlesource.com/chromium/src/%2B/04d774d3827c8532b1b7d3966629f9193a35dd0e/sandbox/win/src/app_container_test.cc): in-memory descriptor and required mask2.
- [CreateProcessW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw): suspended launch, explicit environment and retained handles.

## Approved console-setting experiment — completed once

Connor explicitly approved the temporary LowBoxConsoleEnabled diagnostic. The
single run used the same probe source SHA above and fresh identities:

- Task: `OMS GPT LPAC Console f4eb50a9ed74`.
- Profile: `oms-lpac-f4eb50a9ed744117be1580815b357fe4`.
- Remote root: `C:/Users/conbr/AppData/Local/OMSStudyHub/acceptance/lpac-f4eb50a9ed74`.
- Inner wrapper SHA256: `e23a2cd6e70c22fec38b6c5e2e30bbfce1de3a3b0a6a5d2e8e69be38b1a3f833`.
- Outer wrapper SHA256: `a6b9e629dcef7f723dc41c319d60bbac26421607915886c8378e4200d49fae87`.

Independent wrapper review passed before execution. Both the existing conbr SID
and original value absence were checked before setting only
`HKEY_USERS\S-1-5-21-2532054349-1599019584-1571196523-1004\Console\LowBoxConsoleEnabled`
to DWORD1. The value was recorded enabled at `2026-09-12T15:50:24.7832281Z`.

**The setting did not resolve this startup failure.** The unchanged probe passed
caller3/child2, AppContainer1, exact SID, zero capabilities and session1. Child21176
resumed and exited `0xC0000142` with empty stdout/stderr, then was reaped normally.
Native run: `15:50:31.8539749Z`–`15:50:31.9016430Z`. No timeout or kill occurred.
The synthetic read-boundary oracle correctly failed; no provider was called.

Unconditional cleanup restored original absence at
`2026-09-12T15:50:39.0049967Z`. A separate read-only SSH query confirmed absence at
`2026-09-12T15:51:05.3547495Z`. Independent review confirmed both results.
Both inner and outer preservation checks passed. Latest outer postflight
`2026-09-12T15:50:39.4735015Z` found no new/missing monitored processes, no cleanup
errors, task disabled, and the same healthy Hub PID8268/buildf487c622/schema31 with
all three workers alive/start count1. The task's last result1 reflects failed
acceptance, not failed restoration. A narrow read-only System/Application query
for15:50:15–15:50:45Z returned no events and did not identify a failing DLL.

Archive: `windows-lpac-console.zip`, 52 manifest-bound files; SHA256
`94a596db0f57387fbde32418c1d919820184f2f46dfecb738fa0cf3a2c65c441`.
Manifest SHA256: `8649bba51f07715adc7d389f2223014c2cab62b800a1259c3ab5e6b0011ab5f7`.
The exact scripts, before/enabled/after registry values, independent restoration
read, raw output, token results, task definition and preservation checks are
retained. All archive manifest hashes and CRCs passed O's verification.

This one-shot approval is consumed. No persistent registry setting, ACL change,
weaker token, second launch, provider acceptance or live deployment followed.
The next useful evidence is the actual failing DLL/initialization operation;
another broad setting change is not justified by the exit code alone. Runtime
activation remains closed. The production source is unchanged by this experiment.
