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

Latest task: `OMS GPT LPAC AccessCheck 3092ba4a06da`, disabled, last result1.
Profile: `oms-lpac-3092ba4a06da4dcabb6e9e3943257907`.
Remote root: `C:/Users/conbr/AppData/Local/OMSStudyHub/acceptance/lpac-3092ba4a06da`.
Native run: `2026-09-12T14:27:10.9826022Z` to `14:27:11.0368589Z`.
No timeout or kill was needed; child reaped. Latest outer postflight
`2026-09-12T14:27:17.0931795Z`: no new/missing monitored processes, cleanup error
null, task disabled, same healthy Hub listener PID8268/buildf487c622/schema31 and
three workers alive with start count1. A narrow read-only System/Application event
query for14:26:50–14:27:30Z found no matching events. It does not identify the DLL.

Latest archive manifest:46 files, SHA256
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

## Concrete next diagnostic — not executed

Read-only inspection at `2026-09-12T14:30:43.3890405Z` found no
`LowBoxConsoleEnabled` value in conbr's existing Console key. Microsoft's
[LaunchAppContainer sample](https://github.com/microsoft/SandboxSecurityTools/blob/main/LaunchAppContainer/LaunchAppContainer/LaunchAppContainer.cpp#L423)
sets this DWORD to1 to enable lowbox console processes. That is a supported lead,
not proof that it causes this failure with CREATE_NO_WINDOW.

The proposed experiment is one fresh synthetic LPAC probe with the same reviewed
source/hash/identity/oracle while temporarily setting only
`HKEY_USERS\S-1-5-21-2532054349-1599019584-1571196523-1004\Console\LowBoxConsoleEnabled`
to DWORD1. Check that it is still absent immediately beforehand, retain the
before/after value/type, run the existing bounded watchdog, then restore absence
in unconditional cleanup and verify restoration plus Hub/process preservation.
If it exists or changes concurrently, stop rather than overwrite it. Retain the
fresh task/profile/fixture and raw failed or successful output. No provider,
credential, ACL, firewall or live Hub change is included.

This changes an account-wide Windows setting, beyond the prior fixture-only
mutation boundary. Approval is required before executing that mutation. No such setting was changed during this investigation. The diagnosis
archive retains the exact read-only query and its result.
