# O bounded Windows/runtime acceptance — September 11 ET / September 12 UTC

Code candidate: `a699f7a39f4da562bf057ffc941961cbd7b3dd8f`; tree `7f3fb7930c381805938a0440463305d3afd4c107`; branch `codex/gpt-runtime-activation`. Base for this turn: `e7c84c56bd56a6ad2de412718d38dfff1799d0ed`. Final receipt/status documentation follows the code candidate.

## Scope and reviewed changes

Connor authorized continuing review and bounded Windows/runtime acceptance. O used isolated candidate source and synthetic material only. No live Hub restart/deployment, provider generation, private-source test, Anki operation, paid fallback, reset, purchase, push/main merge or retained-resource cleanup occurred. No explicit account/ACL/firewall/registry mutation was issued. The later follow-ups created, ran each once and disabled only new no-trigger diagnostic tasks; it did not change the live task. The unexpected initial sandbox invocation could perform internal preparation; its possible setup side effects were not independently audited.

- `38e3c179` / `477a06b3`: integrate B's exact GPT-5.5 no-environments diagnostic and receipt. Both effective model tool registries are empty; a valid custom `apply_patch` call is rejected by the native router before file creation. The combined experimental request shape and pinned macOS runtime passed this route. This is neither a real provider response nor Windows isolation proof. Existing Astra async-input/agent-list counterexamples remain valid. [B exact proof](B-gpt55-no-environments.md).
- `f217e434`: nested Office cleanup always attempts Quit and CoUninitialize after Close/Quit errors; a Close failure previously skipped the remaining cleanup. Two regression cases failed before the fix. Extends the existing native Windows smoke to two slides, a visibly sized embedded red rectangle, a blue vector oval, exact image/ZIP/JSON/PDF parity, source labels and correct/incorrect grading.
- `d9c99d98`: integrate B's version-matched, read-only Windows runner diagnosis. [Exact source references and proposal](B-windows-runner-diagnosis.md).
- `b1fc725c`: fix the actual Windows export failure using extended absolute output paths before validation, writing and returned-path reads. Full hashes, artifact identity and trust checks are retained; no machine long-path policy change. Input paths and the caller's output-parent preflight are outside this output-only fix.
- `8de86582`: existing JSON export tests explicitly read UTF-8 on Windows.

Independent reviewer `/root/review_o1_schema` returned final PASS for the exact code candidate, every production/test delta, both evidence ZIPs, all81 export-manifest files, retained PDF/color counts, before/after snapshots and final release/status/receipt documentation. It also reviewed archive contents and the final bounded invocation. Its image-color and postflight enforcement findings were corrected before the relevant acceptance phase. No runtime generation controls were wired into production; `capability_unverified` remains enforced.

## Actual Windows Office/image proof and first failure

Host: `Connors_NUC`, SSH identity `conbr` with an administrator token. This identity is not restricted-runtime acceptance. Python: existing `C:\Services\oms-study-automation-v2\.venv\Scripts\python.exe`, 3.12.10, with candidate PYTHONPATH, `-B`, plugin autoload disabled, and unchanged TEMP for the shared Office lock. Pillow12.3.0, PyMuPDF1.28.0, python-pptx1.0.2, pywin32312, reportlab4.5.1, pypdf6.14.2 were already installed; no dependencies changed.

Stage: `C:/Users/conbr/AppData/Local/OMSStudyHub/acceptance/office-f48247daf993`. Immutable staged candidate `f217e4344fecb67507b96e500aaf018df8b54769`; archive SHA256 `860528f83a3c8b2f43d176b77a2f458604f26175823a2a57234ad1194263ea30`.

Exactly one `SerialOfficeConverter(timeout_seconds=120, admission_timeout_seconds=10)` invocation exported the synthetic PPTX. Its PDF has two pages with the correct slide text. Rasterized assets have distinct hashes, correct slide locators and sanitized byte identity. Each slide retains >1,000 pixels of its intended red/blue shape and zero pixels of the other color. O also visually inspected both retained PNGs. Office process snapshots were empty before and after; no process kill was needed. Timeout/forced-reaping behavior is not established by this successful-close proof.

The initial combined suite returned **7 failed, 11 passed, 2 deselected in 15.51s**. Conversion and image assertions passed; export rename failed with WinError3 at destinations beyond260 characters. Read-only registry inspection found `LongPathsEnabled=0`. Raw failures remain retained. The two omitted tests require symlink capability; their Windows acceptance is not claimed.

Retained native PDF SHA256: `4760dc7bfcc26540aed04f995ec29db6a1e2137ada039e7eb484f4658cd11430`.

## Actual Windows export continuation — passed

New stage: `C:/Users/conbr/AppData/Local/OMSStudyHub/acceptance/export-796ff9295097`. Candidate `8de86582`; archive SHA256 `96c04e0d9fd82669609a526d728422e7a2f71139d0636d60b5a7d453174bb0fc`. Reviewer independently matched every one of353 archive members to Git.

Command: existing Python `-B -m pytest -o addopts= -p no:cacheprovider --basetemp <new-stage>/artifacts tests/study_generation/test_quiz_export.py -k 'not test_export_rejects_indirection_and_preserves_existing_bytes' -q`.

**18 passed, 2 deselected in3.78s**, including actual >260-character Windows output paths, idempotent exports, matching questions, medical superscripts/subscripts, Unicode, embedded font files, full payload/image/provenance parity, corrupted immutable output rejection and unsupported glyph rejection. This local-drive proof does not establish UNC or Windows junction/symlink behavior.

A separately hash-pinned continuation patched the converter only to copy the retained native PDF, then exercised the same two-slide raster/image/export/grade assertions. It passed with **zero additional Office launches**. The retained PDF hash was checked before and in finally after the phase. This composes the earlier actual Office conversion with the corrected export code; it is not a second end-to-end native conversion run or provider generation.

The first attempt to transport the enlarged PowerShell script inline failed at parsing (10,988 encoded characters). No phase executed. The exact same reviewed script was transferred as a file, hash-verified and executed via a short wrapper; the original parser error is retained. Final verification/test exits were0 and `postflight_ok=true`.

Postflight `2026-09-12T03:08:53.2049179Z`: one old-Hub listener PID8268; commit `f487c6229b91d2b1ac11729e465561f6c1ffe997`, tree `a9f7cc7f9c03040cb63ccea3ef05b70add443e6a`, schema31; healthy workers with unchanged start counts, no current errors; dirty manifest unchanged; no Office processes. Exact task/owner inventory and all before/after data are retained. Nothing was applied to the live checkout.

## Windows Codex runtime — caller loading accepted, restrictions pending

Follow-up base: `5ddefba39846b8becbc1c5bdb18ac193d1a76010`; B source findings integrated as `28723a02`, `b4f73d6b`, `009eda59`, `08f03f30`. At that caller-loading checkpoint, application source, tests, scripts and configuration remained byte-identical to code candidate8de86582. The subsequent registry-only diagnostic below adds a separate script and tests; application source is still unchanged. This follow-up adds source diagnosis and bounded native diagnostics, not production activation.

Working standalone `C:\Users\conbr\.local\bin\codex.exe`: `codex-cli0.153.4`, SHA256 `444a3f0008050605cae73cd9b7a2dcac61294062dfaab56dd20430fd6498518b`. MSIX launch separately returned access denied; no bypass was attempted. The signed helper at `C:\Users\conbr\.codex\.sandbox-bin\codex-command-runner-0.153.4.exe` hashes to `8d88f3e1749704937e735fe2b0311e939bd2873010243030a6f0c023d3d7296d`.

The initial incorrect `sandbox windows --help` invocation treated `windows --help` as the target. Its runner timed out before target-request delivery. Follow-up found System/Application Popup event26 at `2026-09-12T02:48:48.491Z` naming this helper version and `0xc0000142` (`STATUS_DLL_INIT_FAILED`). This is actual initialization-failure evidence, not proof of the failing DLL, desktop ACL cause or exact PID correlation. Application crash/WER/SxS queries had no matching events. The helper and containing runtime directories grant RX to `CodexSandboxUsers`, whose Offline/Online accounts are enabled members; this does not establish their effective startup access. The narrow helper-selection log filter returned no lines.

Crucially, SSH runs in session0 with an administrator token. The live Hub runs in session1 as `conbr`, with task principal InteractiveToken (3), Limited (0). That is a proven context mismatch, not an established cause of the old failure.

Two independently reviewed, exact-hash **direct helper/no-argument** checks passed:

1. SSH/conbr: helper PID7552, expected exit1 and exact stderr `Error: runner: no pipe-in provided` plus newline; no timeout; helper processes absent afterward and Hub postflight passed.
2. Intended caller context: new on-demand task `OMS GPT Helper Acceptance 8627d0cba8c8`, no triggers, conbr/InteractiveToken/Limited. Runtime guard confirmed `CONNORS_NUC\conbr`, session1, administrator token false. Helper PID13776 returned the same expected exit/message. Task result0; inner and independent outer Hub/helper/worker/listener postflight passed. Only this new task was disabled afterward and retained. No live task was changed.

The version-matched source exits on absent arguments before pipe opening, credential/setup/log initialization, token creation or target dispatch. Loader/CRT/Rust initialization still necessarily occurs. Each probe used a fresh fixture and minimal inherited environment,15-second own-process timeout, raw stdout/stderr and exact hash checks. The task wrapper independently checks preservation even if its inner process is stopped. These results establish helper entry under the intended caller identity; they do **not** establish Offline-account loading, runner IPC, tool prevention, network/read-root isolation or provider readiness.

The normal sandbox CLI retry was rejected after inspecting actual release behavior: it unconditionally refreshes ACLs even with existing setup, passes empty deny-read overrides, and reconciles away previously recorded SID-owned deny-read paths absent from that set. Missing/incompatible setup can run full elevated preparation. No supported state-only/no-setup switch was found for that normal CLI route; sandbox-state-json converges on it. The read-only profile's Restricted network setting also retains matching stored proxy/local-binding exceptions. O did not rerun this path, change accounts/ACLs/firewall, read credentials or select a weaker fallback. Possible internal state changes from the earlier unintended invocation remain unaudited. [Exact version-matched sources and scope assessment](B-windows-runner-diagnosis.md).

Evidence root: `/Users/connor/.codex/visualizations/2026/09/11/01a09271-e627-7712-a2fc-ff675db23a8b/windows-runner-followup`. It contains native pre/post/result files, System event and session/ACL metadata, both reviewed probes, task wrapper/invocation, disabled task XML and a checksum manifest. The SSH script SHA is `a435d045eab36159e8fefe74beb826f5ef7fc4f164d4502d6ebd72ee72552eb9`; interactive script SHA `e372f741e8fd742b4c2c22f8c5137dfd83ae2a6028db937aa70db2a05f88f180`; task wrapper SHA `1418bedc17e6b630aa560d4a4c1dadffcf21b1fcafc2b39d3c6f35141b89ba3e`.

Independent final review returned PASS: all32 manifest files, exact raw helper results, disabled/no-trigger task XML and both inner/outer Hub postflights were verified. Evidence manifest SHA256: `4893780df419b6b08c620f5a08d2f19080d7640ba42ac347d3767faf9b3ae3e4`. Final interactive inner postflight: `2026-09-12T03:32:43.0392493Z`.

Next runtime work must use a supported app-server/restricted-identity path that preserves existing access rules, with its startup side effects reviewed before a native launch. The rejected debug CLI is not a substitute for that proof. No provider turn or additional login was requested or performed.

## Local checks and evidence

- Office regression:19passed1Windows skip; reviewer independently reproduced.
- Broader Office/document/export/runtime regression atf217e434:197passed5skipped in21.93s.
- Final export/atomic/private route checks at8de86582:26passed1Windows skip in3.15s.
- Full Ruff source/tests/scripts and mypy source plus diagnostic:PASS,231 files. Base full3821Python/284JavaScript remains prior evidence, not a new full-suite run.
- A local diagnostic using Python3.13/PyMuPDF with global warning-as-error exited139; the ordinary retained-flow check passed once its macOS temporary path was resolved. This does not replace native Windows evidence or establish macOS raster teardown acceptance (already excluded in B7).

Evidence root: `/Users/connor/.codex/visualizations/2026/09/11/01a09271-e627-7712-a2fc-ff675db23a8b/windows-acceptance`.

- `office-evidence.zip`: SHA256 `e0e96778c014d22ccdab386224c3e202b98d7876c8874979f936c6392573d283`; initial failure plus retained native images/PDF.
- `export-evidence.zip`: SHA256 `a53abfb9c2ef2a79c6c23da5169745d130af6313c1e2a95c2e3e856cd79416df`;81 files independently checksum-verified locally, including pre/postflight, logs, accepted outputs and invocation sources.
- `corrected-help-and-helpers.stdout`, `windows-sandbox-help.stderr`, all raw transport outputs and stage manifests are retained.

Final local preservation check: original checkout remains at `96ceec56c4b8f9df0018fe090710631adb29a2a2` with the exact O0 dirty manifest; the unchanged port56460 preview is healthy, schema40, all three workers alive with start count1 and no current error.

Next: review this candidate, then resolve the Windows runner startup/identity boundary before any account/session/provider activation. Windows login persistence, restricted readable roots, provider text/image/schema/cancellation/limit behavior and deployed behavior remain pending. The working Mac preview needs no new login. AMBOSS entitlement/reply, vendor samples and real Anki QID-prefix rules remain separate optional pending capabilities.


## Windows app-server registry-only acceptance — September 12

B implementation `f1f799af7a904752fd2822df83c9f50008519964` and receipt
`a53f3d6af361c5893aba02668f867d7e84151bbd` were integrated as `551339cb` and
`ac3e7c5d`. The separate stdlib probe preserves the macOS confinement guard and
production `capability_unverified` gate. Source review found that this exact
empty-environment startup path avoids the normal debug CLI's shared setup/ACL
refresh. [Source scope and implementation receipt](B-windows-appserver-no-tools-scope.md).

The independent review corrected early completion before the turn/start reply,
incremental evidence retention, and the outer descendant-survivor check before
launch. O independently passed **7 new offline tests in 0.54s** and Ruff; B's
focused suite passed **77 tests with 3 opt-in skips**. Application source and the
Mac probe remain byte-identical to `8de86582`. No full suite was rerun.

One actual Windows run passed under `CONNORS_NUC\conbr`, session1, nonadministrator:

- Exact pinned Windows Codex0.153.4, fresh private file-only auth home and minimal
  environment; `windows.sandbox=elevated` retained as configuration.
- Only initialize, ephemeral thread/start and turn/start RPCs, with GPT-5.5,
  explicit empty environments/capability roots/workspace roots/dynamic tools.
- One auth-free `/v1/responses` request to the loopback fixture; top-level tools
  empty and no additional-tools registry; fixed assistant text only, no injected call.
- Completed correlated turn; native PID15772 exited0, no error or inner timeout.
- New no-trigger task `OMS GPT Registry Acceptance 3fb47f0f1105` returned0,
  was disabled and retained. Inner and independent outer postflight passed;
  no new Codex/Python/helper processes survived. Hub PID8268/build/tree/schema31
  and healthy idle workers/start counts were unchanged.

The local SSH caller timed out after125s. Its initial transport stdout/stderr
were not saved by the launch wrapper, so no successful transport claim is made.
The remote incremental/final files were recovered without rerunning the probe.
Outer postflight was `2026-09-12T07:36:10.8375917Z`; independent reconciliation at
`2026-09-12T08:03:45.7678533Z` confirmed task result0/disabled and healthy old Hub.
Native acceptance is based on the recovered files and task definition.

Evidence root: `/Users/connor/.codex/visualizations/2026/09/11/01a09271-e627-7712-a2fc-ff675db23a8b/windows-registry-acceptance`. The43-file manifest SHA256 is `49ae84e8b5bbf8a37e4be08cad830d282e349d5ae8542b0a7cc2a1a177a67daa`;
it includes raw protocol, fixture request/response, prelaunch settings, native PID,
inner/outer pre/postflight, disabled task XML and reconciliation. Probe SHA256:
`2c27997fba090aa496c37c7096c755b8db4cfeae83fcb8a80f9d4052c4afa339`.
Independent reviewer `/root/review_windows_registry_final` returned PASS for the
prelaunch code/task and all43 recovered manifest files, raw protocol correlation,
empty registries, native exit, task definition and independent preservation checks.

This proves the measured registry-only path on Windows. It does not prove
injected-tool denial, universal feature coverage, OS network/read-root isolation,
Offline-account execution, account persistence, provider quality or deployment.
Those flags remain false; no real account/provider request occurred. Normal
sandbox CLI retry remains rejected. The next runtime step must separately review
any injected-tool or restricted-identity scope before execution.


## Windows single-call patch denial — September 12

Code `a699f7a39f4da562bf057ffc941961cbd7b3dd8f` adds `--mode apply-patch` to the existing standalone
probe. The default registry mode remains available. The mode sends one fixed,
valid Add File patch aimed solely at the fresh fixture's `work/tool-must-not-create`.
It refuses to inject when either effective registry is populated; accepts a
second fixture request only with the exact echoed call and correlated
`unsupported custom tool call: apply_patch` output; and requires `lexists` to
confirm the canary is absent. It retains both requests/responses and reports a
separate `apply_patch_denial_passed` flag. Production source, Mac probe, account
configuration and all activation flags remain unchanged.

Independent pinned-source review found registration gated by available
environments, and missing-runtime rejection before hooks or the handler in
[spec_plan.rs](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/tools/spec_plan.rs#L1239)
and [registry.rs](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/tools/registry.rs#L516).
The correlated error return has no executor fallback on the reviewed route:
[parallel.rs](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/tools/parallel.rs#L219).
Source, implementation and exact wrapper review passed before launch.

Local test-first fixture failed before implementation. Final candidate-source
checks passed **78 tests, 3 opt-in skips in4.77s**; Ruff and diff checks passed.
An initial broader collection attempt resolved the original checkout's installed
package and failed before tests; explicitly selecting candidate `PYTHONPATH=src`
corrected the harness. Both outputs are retained. No full application suite or
real provider call was repeated.

One Windows native run passed as `CONNORS_NUC\conbr`, session1/nonadministrator,
with the same pinned Codex0.153.4, fresh file-only auth home, empty environments
and roots, explicit controls and elevated Windows setting. Native PID16132 exited0
with a correlated completed turn. Both actual requests offered no tools; the
second contained the exact injected custom call and rejection. The canary was
absent. This is actual pre-handler rejection for this call, not a model choosing
to refrain from using tools.

New task `OMS GPT Patch Acceptance cdf2b00b33fc` ran once, returned0, and is disabled
with no triggers. Inner and independent outer postflight passed; no new
Codex/Python/helper processes survived. Hub PID8268, build/tree/schema31 and
healthy idle worker start counts remained unchanged. Outer postflight:
`2026-09-12T12:36:11.3881979Z`; independent reconciliation:
`2026-09-12T12:36:58.3106219Z`. SSH transport also exited0 without timeout; the
launch wrapper now streams raw transport output to retained files from launch.

Evidence root: `/Users/connor/.codex/visualizations/2026/09/11/01a09271-e627-7712-a2fc-ff675db23a8b/windows-apply-patch-acceptance`. Manifest43-file SHA256:
`f777d374c779c3d548e2997084eb2b7f7a9d380e4aa50504b56ff49d4b8b0c1c`. Script SHA256:
`773d0fabc29438d0e1cdc1815e1fac187892aa2fd7f45aa826c7facc8aeaff1e`.
Independent reviewer `/root/windows_patch_scope` returned PASS for all43 files,
raw protocol and call correlation, exact task definition, native/transport exit,
and corroborated identity/process/Hub postflight.

Remaining B1 gates are all-tool-class prevention, restricted OS identity/readable
roots/default-platform-root exclusion, managed Windows login persistence and
real synthetic provider text/image/schema/interruption/limit acceptance. This
single-call result does not satisfy those gates. Normal sandbox CLI retry still
crosses the rejected shared ACL/setup boundary. No live Hub restart/deployment,
private source, paid fallback, reset, purchase, Anki mutation or retained-resource
removal occurred.
