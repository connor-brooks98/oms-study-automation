# O bounded Windows/runtime acceptance — September 11 ET / September 12 UTC

Code candidate: `8de86582e1f9ca12bfc00119121c2628542fe43b`; tree `7aa5bed47d3bb15d130088c13d81c9b3808b1073`; branch `codex/gpt-runtime-activation`. Base for this turn: `e7c84c56bd56a6ad2de412718d38dfff1799d0ed`. Final receipt/status documentation follows the code candidate.

## Scope and reviewed changes

Connor authorized continuing review and bounded Windows/runtime acceptance. O used isolated candidate source and synthetic material only. No live Hub restart/deployment, provider generation, private-source test, Anki operation, paid fallback, reset, purchase, push/main merge or retained-resource cleanup occurred. No explicit account/ACL/firewall/registry mutation was issued. The unexpected initial sandbox invocation could perform internal preparation; its possible setup side effects were not independently audited.

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

## Windows Codex runtime — not accepted

Working standalone `C:\Users\conbr\.local\bin\codex.exe`: `codex-cli0.153.4`, SHA256 `444a3f0008050605cae73cd9b7a2dcac61294062dfaab56dd20430fd6498518b`. The MSIX executable separately returned access denied; no ACL change or bypass was attempted.

O's initial `sandbox windows --help` invocation was incorrect: the version-matched parser treats `windows --help` as a target command. Native execution stopped with `windows sandbox failed: timed out after 15000ms connecting runner pipe-in`. Source places this after runner process creation and before delivery of the target request. Actual startup/pipe failure cause is unknown; source cleanup intent does not prove host cleanup. No target-command or provider execution is established.

Corrected `sandbox --help` subsequently exited0 and returned CLI metadata only. Read-only helper inventory found signed `codex-command-runner-0.153.4.exe` in `.codex/.sandbox-bin`, SHA256 `8d88f3e1749704937e735fe2b0311e939bd2873010243030a6f0c023d3d7296d`, created September7. That does not establish which helper the failed launch selected or why it failed. No sandbox retry, explicit setup/account reconciliation, legacy fallback or broader permission repair was performed. The proposed next native command and possible setup side effects are documented in B's diagnosis for separate scope review.

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
