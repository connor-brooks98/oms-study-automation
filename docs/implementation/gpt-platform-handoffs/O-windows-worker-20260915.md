# Windows managed worker — 2026-09-15

Connor authorized completion of the Windows worker. Linux hosting is deferred for several months.

## Candidate behavior

The shared queue/session client launches the accepted private Codex executable through a native LPAC process on Windows. No new Python dependency, API billing path, database migration, or replacement queue is introduced.

- Runtime SHA256: `d6eb90b7409dc22f407a9dfa44ec629a8a5a6bf3ca001493aef13dd85a75dbda`, version 0.153.4.
- Accepted model: **gpt-5.5**. GPT-5.6 Terra was tested and failed the empty-registry assertion: it advertises `functions.exec` despite disabled features. It remains unavailable through this worker.
- Each process gets a fresh AppContainer identity. Only the dedicated session home is writable; the current staging directory and runtime are read-only. Prior crashed-request grants do not authorize a new identity.
- The parent verifies non-elevated caller, LPAC child identity/capabilities/session, and functional access masks before resuming. The creation-time job list closes the parent-death gap; descendants are prohibited. Normal cleanup revokes grants, deletes the owned profile, and closes handles.
- Four exact capabilities: internetClient, lpacIdentityServices, lpacCom, registryRead. The additional identity capability is necessary for Schannel HTTPS: the three-capability control failed with `SEC_E_SECPKG_NOT_FOUND`; adding only lpacIdentityServices enabled verified HTTPS. No firewall, persisted execution-policy, shared AppContainer grants, or TLS-verification changes.
- The existing managed account persists in the dedicated home. Status uses an empty connection directory rather than granting access to the entire work root. Unexpected config.toml is refused; runtime hash, canonical NT home, version, account, model modalities, and policy echo are checked.

Native API references: [Microsoft process attributes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute), [AppContainer launch](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer), and [Chromium network capability configuration](https://chromium.googlesource.com/chromium/src/+/lkgr/sandbox/policy/win/sandbox_win.cc).

## Native acceptance

- Native limited interactive account: **290 passed, 4 skipped** after the live-directory correction, including file boundaries, stale-request isolation, writable-runtime rejection, and parent death before child resume. Optional schema/old native probes/browser checks were skipped; the explicit current native probes below ran separately.
- Final four-capability synthetic provider matrix: empty registry and all 15 exact correlated unsupported-call results; completed turn and normal exit 0.
- Final four-capability controlled interrupt: the fixture received the provider request before the interrupt trigger; response body withheld; empty acknowledgement and matching interrupted turn; normal exit 0. The fixture also proves client disconnect and terminates cleanly; the earlier fixture deadline failure is retained separately and is not acceptance.
- Actual subscription: one completed GPT-5.5 synthetic text/image/schema turn returned exactly `{"number":7,"color":"red"}`. A fresh process retained the account. Source credentials stayed unchanged; all three process handles/jobs and temporary profiles were cleaned up.
- Windows-targeted strict mypy and changed-file Ruff passed. Mac regression: 285 passed, 9 native/optional skips. Model-aware queue regression: 97 passed on Windows and Mac. Managed Hub timeout/settings checks: 83 passed, 1 optional skip on both platforms.
- Independent read-only review found four ownership issues during development; all were corrected and reviewed with no remaining blockers.

Initial failed diagnostics are not acceptance: elevated SSH was refused, the old LAN fixture address was stale, a private-network-only capability did not reach the Tailscale fixture, Terra exposed an execution tool, three-capability HTTPS failed, and an unsanitized synthetic image was rejected before dispatch. Native regression staging initially omitted probe scripts and two updated tests; the complete final candidate passed. The first clean Mac environment lacked the optional existing AnyDoc dependency; final local checks use the established development environment.

Evidence is retained in the Windows candidate directory `C:\Users\conbr\AppData\Local\Temp\oms-win-20260915a`, plus the [selected evidence archive](evidence/windows-worker-20260915.zip) (SHA256 `c80bda233a10a836cfcd6955d459bfbfb84ee904acdd41c4ae23b9208b1bd85a`). Credential-bearing homes and runtime executables are excluded from that archive.

## Live corrections and deployment

- Final code release: `3480372bf885d9541b3259a2bb9dc7007b19a4ff`, tree `8854d82a306ad4aa4033e6bd9e6b999c5963ee28`. GitHub main and the Windows checkout contain this implementation.
- Runtime: `C:\ProgramData\OMSStudyHub-V2\runtimes\codex-d6eb90b7409d\codex.exe`, same accepted SHA256 above. Existing subscription login is connected; selected model is GPT-5.5. No paid API fallback.
- Python's Windows `mkdir(mode=0700)` creates protected child ACLs. The live temporary folder therefore blocked the root's temporary package grant. Keep the session/work roots private, let their descendants inherit, and repair only the five known existing host-home directories after saving original SDDL. Native checks now exercise production-created roots, SQLite WAL persistence across fresh identities, temporary writes, outside/staged-write denial, and descendant grant cleanup.
- Process termination can return access-denied while an already-exiting child remains unsignaled. Wait for confirmed exit within the existing shutdown bound; a still-running process remains an error. Both cases are tested.
- The old runtime's five state databases passed integrity checks and contained zero chat threads, but their migration checksums differed from the accepted private runtime's databases. Archived all 15 database/sidecar files, retaining every byte and the existing auth file, before compatible state initialization. Native archive/restore checks prove byte preservation and refusal to restart when original-state verification fails. No migration checksums were rewritten.
- Automatic quiz deduplication now includes the frozen model, preserving old attempts while allowing Generate to use a newly selected model. The Hub permits up to 600 seconds per managed turn; the separate worker health bound still covers the whole run (900 seconds), so full-run duration and readiness are observed during acceptance.
- The initial rollout hit PowerShell's null-string overload in `File.Replace` and safely rolled back. An explicit backup path fixed it. A later preflight caught a busy worker without stopping it. Final rollout completed with 17.448 seconds measured downtime; all three workers passed exact revision/tree/schema readiness checks.

Final deployment receipt: `C:\ProgramData\OMSStudyHub-V2\backups\windows-worker-20260915-a656f20fffea409d976ad6b5b12d0c31\code-deployment-result-20260915T204130419Z-8fa409d8321749c0843767f42bd61cf3.json`.

Preserved old runtime state: `C:\ProgramData\OMSStudyHub-V2\backups\windows-worker-20260915-ae09b90f255c4fc59a65304a6f0a5815\prior-runtime-state`.

## Real lecture acceptance

- Original Terra run `37da3003-83bf-4496-892a-56f2fcaefbf2`: cancelled through the normal endpoint after verifying no provider attempts. Its frozen settings remain intact.
- First full GPT-5.5 lecture run `ab2c8ac7-47ca-4aaa-9e2b-c6ed6f048fbc`: reached the provider, then hit the old 180-second planning deadline. Retained as interrupted; never described as passing.
- Fresh normal Generate request after the timeout and deduplication fixes: `c190d97a-9a43-4c39-bd59-59b5360587a5`, Cardio exam 1, lecture 1. **Passed generation-to-review acceptance.** Planning and both question batches completed; the run reached `awaiting_review` with no error. The final artifact has 24 unique questions, four choices each, 25/25 coverage groups represented, citations and answer/distractor rationales for every question, and 12 questions with lecture images. These are structural checks, not medical answer verification.
- Readiness remained `ok` throughout the observation window; maximum sampled active duration was 688.641 seconds, below the existing 900-second whole-run health bound. Longer jobs remain subject to that bound. The owned Codex child was reaped after completion.
- The authenticated public review page rendered all 24 questions and source/image controls. Review returned HTTP 200; preview and JSON/PDF/ZIP exports correctly returned HTTP 409 because all answers remain unverified. No publication or download acceptance is claimed.
- Selected model is GPT-5.5; the artifact retains `actual_model_evidence=unverified`, rather than inventing provider model attestation.
- Removed the three synthetic-provider credential copies after testing; the protected live login remains connected.

Linux migration remains deferred in `docs/future-implementation-ideas.md`.
