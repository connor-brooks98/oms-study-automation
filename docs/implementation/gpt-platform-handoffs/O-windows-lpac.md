# O: bounded Windows LPAC probe

2026-09-12. **Prepared only; Windows compilation, startup and isolation acceptance
have not run.** This is a network-free feasibility test, not a production launcher
or GPT activation receipt. Production remains `capability_unverified`.

## Exact scope

`scripts/probe-windows-lpac.ps1` runs in an existing 64-bit Windows PowerShell
Desktop process. The native portion rejects an elevated caller. It accepts an
existing local fixture parent and a never-used name matching
`oms-lpac-` plus 32 lowercase hexadecimal characters. Existing fixtures/profiles
are errors, never reused. Reparse-point ancestors are rejected.

The only native child is the existing System32 `cmd.exe`, with fixed arguments:

```text
/d /v:off /c "type inside.txt & type ..\outside.txt"
```

Both files are newly generated synthetic canaries. The working directory is the
fresh fixture's `inside` directory. No caller-supplied command text, Codex launch,
auth, provider, sandbox setup, network request or loopback exemption is involved.
O's outer wrapper must independently check the intended conbr/session1/Limited
identity and Hub/process preservation before and after this invocation.

## Mutations and lifetime

- A new fixture directory receives a protected caller-only DACL. All canaries,
  compiler temporary files and raw evidence are new files under it. TEMP/TMP are
  changed only within the calling process for compilation, then restored.
- `CreateAppContainerProfile` creates a new per-user profile: directories **and
  registry storage**. The profile name and package SID are retained in
  `profile.txt`. No account creation or managed sandbox credential access occurs.
- Only new fixture ACLs change: the new package SID receives non-inherited
  traverse access on the fixture and inherited read/execute access on `inside`.
  The sibling `outside.txt` and raw output files receive no package grant. No
  Everyone, logon SID, ALL APPLICATION PACKAGES, shared Windows directory, shared
  account ACL or firewall rule is changed.
- The profile and fixture are retained on success and failure. This script never
  deletes an AppContainer profile, retries, or selects ordinary AppContainer as a
  fallback.

## Mandatory boundary and acceptance

The launch uses `SECURITY_CAPABILITIES` with **zero capabilities**, the LPAC
`ALL_APPLICATION_PACKAGES` opt-out, an explicit three-handle stdin/stdout/stderr
inheritance list, and a policy prohibiting descendant processes. The child gets
only COMSPEC, SystemRoot, TEMP, TMP and WINDIR in its explicit environment.
Standard streams are handles to new caller-private files; those inherited
handles intentionally permit only this child's input/output without granting
file-path access to the evidence directory.

`CreateProcessW` uses `CREATE_SUSPENDED`. Before `ResumeThread`, the parent records
`token.txt` and requires `TokenIsAppContainer=1`,
`TokenIsLessPrivilegedAppContainer=1`, zero token capabilities, and an exact match
between the child package SID and the newly created profile SID. An unsupported
attribute/query or mismatched token fails closed and terminates the suspended
owned process. Enum values are 29, 46, 30 and 31 respectively.

The child has a 15-second wait limit. On failure/timeout, termination uses only
its retained process handle, followed by a 5-second reap wait. Handles and native
allocations are released. An unreaped child is explicitly reported as failure;
O must retain the outer process watchdog and preservation checks.

Success requires all of the following in `result.json`:

- Child exited 1 after the expected denied second TYPE command.
- Raw stdout is exactly the inside canary and CRLF; the outside marker is absent.
- Raw stderr, trimmed, is exactly English `Access is denied.`. Another locale or
  any other error fails; it is not accepted as proof of access denial.
- Both canaries retain their exact original content.
- Token checks passed, the child was resumed once and then reaped normally.

Raw `child.stdin`, `child.stdout`, `child.stderr`, `profile.txt`, `token.txt` and
`result.json` remain in the fixture when their respective stages were reached.
Failures before a stage may lack its artifacts; the retained stage/error names
identify the boundary. PowerShell compilation failures are recorded in
`result.json` too.

## Validation and next boundary

The script itself is the runnable acceptance check. No native Windows call or
remote call was made while preparing it. This macOS environment has no PowerShell
or C# compiler, so even Windows compilation is pending. Source review does not
prove LPAC cmd/DLL startup or readable-root enforcement. There is deliberately no
second Codex child or automatic compatibility workaround.

A passing canary probe establishes only this new LPAC's synthetic read boundary.
Pinned Codex startup, all-tool-class prevention, managed credential separation,
login persistence, network/provider operation and production activation remain
separate gates. The approved plan's OS identity requirement is not replaced by
empty Codex execution environments.

## Primary sources

- [Microsoft LPAC launch procedure](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer):
  SECURITY_CAPABILITIES, the extra opt-out attribute, and CreateProcess.
- [CreateAppContainerProfile](https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-createappcontainerprofile):
  new per-user folders/registry storage and existing-profile failure.
- [Token information classes](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ne-winnt-token_information_class):
  AppContainer SID, capability count and LPAC identification.
- [Process attributes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute):
  explicit handle inheritance and child-process policy.
- [CreateProcessW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw):
  suspended creation, explicit application/command/environment, and process handles.
