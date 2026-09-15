# Windows managed worker — 2026-09-15

Connor authorized completion of the Windows worker. Linux hosting is deferred for several months.

## Candidate behavior

The shared queue/session client launches the accepted private Codex executable through a native LPAC process on Windows. No new Python dependency, API billing path, database migration, or replacement queue is introduced.

- Runtime SHA256: `d6eb90b7409dc22f407a9dfa44ec629a8a5a6bf3ca001493aef13dd85a75dbda`, version 0.153.4.
- Accepted model: **gpt-5.5**. GPT-5.6 Terra was tested and failed the empty-registry assertion: it advertises `functions.exec` despite disabled features. It remains unavailable through this worker.
- Each process gets a fresh AppContainer identity. Only the dedicated session home is writable; the current staging directory and runtime are read-only. Prior crashed-request grants do not authorize a new identity.
- The parent verifies non-elevated caller, LPAC child identity/capabilities/session, and functional access masks before resuming. The creation-time job list closes the parent-death gap; descendants are prohibited. Normal cleanup revokes grants, deletes the owned profile, and closes handles.
- Four exact capabilities: internetClient, lpacIdentityServices, lpacCom, registryRead. The additional identity capability is necessary for Schannel HTTPS: the three-capability control failed with `SEC_E_SECPKG_NOT_FOUND`; adding only lpacIdentityServices enabled verified HTTPS. No firewall, execution policy, shared AppContainer grants, or TLS-verification changes.
- The existing managed account persists in the dedicated home. Status uses an empty connection directory rather than granting access to the entire work root. Unexpected config.toml is refused; runtime hash, canonical NT home, version, account, model modalities, and policy echo are checked.

Native API references: [Microsoft process attributes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute), [AppContainer launch](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer), and [Chromium network capability configuration](https://chromium.googlesource.com/chromium/src/+/lkgr/sandbox/policy/win/sandbox_win.cc).

## Acceptance before deployment

- Native limited interactive account: **287 passed, 4 skipped**, including file boundaries, stale-request isolation, writable-runtime rejection, and parent death before child resume. Optional schema/old native probes/browser checks were skipped; the explicit current native probes below ran separately.
- Final four-capability synthetic provider matrix: empty registry and all 15 exact correlated unsupported-call results; completed turn and normal exit 0.
- Final four-capability controlled interrupt: the fixture received the provider request before the interrupt trigger; response body withheld; empty acknowledgement and matching interrupted turn; normal exit 0.
- Actual subscription: one completed GPT-5.5 synthetic text/image/schema turn returned exactly `{"number":7,"color":"red"}`. A fresh process retained the account. Source credentials stayed unchanged; all three process handles/jobs and temporary profiles were cleaned up.
- Windows-targeted strict mypy and changed-file Ruff passed.
- Independent read-only review found four ownership issues during development; all were corrected and reviewed with no remaining blockers.

Initial failed diagnostics are not acceptance: elevated SSH was refused, the old LAN fixture address was stale, a private-network-only capability did not reach the Tailscale fixture, Terra exposed an execution tool, three-capability HTTPS failed, and an unsanitized synthetic image was rejected before dispatch. Native regression staging initially omitted probe scripts and two updated tests; the complete final candidate passed. The first clean Mac environment lacked the optional existing AnyDoc dependency; final local checks use the established development environment.

Evidence is retained in the Windows candidate directory `C:\Users\conbr\AppData\Local\Temp\oms-win-20260915a`, plus the explicitly selected local evidence archive. Credential-bearing homes and runtime executables are excluded from that archive.

## Deployment and lecture acceptance

Pending the backed-up rollout and real lecture quiz. The earlier paused Cardio lecture 1 run has frozen Terra settings and no provider attempts; it must not be silently rewritten. A new normal queued run will use the validated model after the Hub setting is updated.
