# GPT Study Hub candidate — release proposal

Status: core implementation and bounded Windows artifact acceptance are ready for review. Actual Windows PowerPoint conversion preserved the embedded image and vector shape; a discovered long-path export defect was fixed, reviewed and passed18 Windows export checks plus retained-slide export/grade parity. The working local preview has a connected ChatGPT account. Generation activation and live NUC deployment remain pending.

Reviewed production-policy commit: `0aa11c7ee40021ede773f6ddc3c9c6e063b557fc`, tree `0614b7a5c19b791e8ca30bd8dafe906ed613236e`, branch `codex/gpt-runtime-fix-2026-09-12`. The requested pre-change snapshot was pushed at `274da9d74108e0e8a4f6b6b51ecec5a9550fdd0b`; `b5d19cd5` additionally backs up both completed Windows raw evidence bundles in Git. The production client now applies the accepted experimental protocol and fixed policy, private process home and platform binary pins, and rejects policy drift before `turn/start`. Independent review passed; O retest passed 97 checks with three opt-in skips, Ruff and strict mypy. The generation readiness guard remains closed. The subsequently approved single temporary LowBoxConsoleEnabled experiment did not resolve `0xC0000142`; the setting was restored to its original absence and independently verified. No production source or activation change followed. [Exact policy handoff](gpt-platform-handoffs/B-runtime-policy.md).

Current source candidate: `f39d78a28f09b51b4b9b1a78c4347d8d4413bfc3`. The reviewed Rust path-resolution patch now passes native Windows LPAC red/green tests, including canonical reopen and unchanged read/write denial boundaries. The Codex temp-containment correction is independently reviewed; the private build reached compilation but Application Control rejected an unsigned dependency helper. No rebuilt Codex was produced; runtime acceptance remains pending. Production generation stays closed. [Current fix and native receipt](gpt-platform-handoffs/O-windows-canonicalize.md). Earlier restricted Codex startup failed with a confirmed MountPointManager denial during DOS canonicalization; [that failed proof remains retained](gpt-platform-handoffs/O-windows-codex-startup.md). Existing release archives below remain bound to original core code `9e54da4188abaa5694c57b0d59aaec7c4e216a4f`, not rebuilt packages for this candidate.

## Intended release and limits

This replaces the required Google path for new lecture quizzes with an explicitly selected Codex subscription backend, preserves full slide/transcript evidence and image assets, requires question/answer review before native publication, and adds private source-scoped/general chat. Outlines are optional. Existing publications, imported artifacts, Anki scheduling and Gate 2B proof resources are retained.

**Activation is blocked, even after local tests pass.** The inspected Codex runtime has not established universal pre-execution tool prevention, restricted Windows identity/readable roots, account persistence, or actual model text/image/schema acceptance. Production generation fails closed with `capability_unverified`; no paid fallback or configuration bypass is present. Fake-client tests prove the application workflow only. A subsequently authorized managed-device login succeeded in the isolated macOS preview on September 11 (ET). The Hub reported the account connected and advertised model slugs. This establishes local login only; generation and Windows account persistence remain unverified.

| Capability | Local candidate scope | External/operational limit |
| --- | --- | --- |
| Full lecture-image quiz | Full approved slides and cleaned transcript, frozen objective coverage, source locators, original sanitized images, native review and publication | Synthetic Windows Office/image/export subset accepted; subscription generation and forced-timeout cleanup pending; conservative request ceiling: 20 images and 100,000 serialized source characters |
| Optional outline | Current-source validation through filing/commit; durable raw result; PDF filing with Codex provenance | GPT replacement of any retained imported NotebookLM outline is explicitly unavailable; historical legacy review is preserved |
| Native JSON/ZIP/PDF exports | Same native payload and original PNGs; source/objective manifest; embedded-font PDF with unsupported-text rejection; private download parity reviewed | Actual Windows18 export checks passed; no private lecture evidence used; UNC and symlink/junction cases unaccepted |
| Lecture/general chat | Owner-scoped current source snapshots, citations, durable history, cancellation/ambiguous-turn protection | Same runtime/account activation gate |
| Question-bank imports | Normalized results-only or explicitly authorized content, preview/confirm, identity conflicts and review | Exact supported UWorld and TrueLearn samples still needed; no guessed proprietary parsers |
| Anki candidates | Exact observed source/product-qualified QIDs and allowlisted pasted NIDs against an existing local index | Real prefix rules/index setup pending; no live Anki changes or full curation |
| Private progress | Server-issued attempt identity, durable exact replay, first/repeat and omitted/unknown counts | Descriptive counts only; no pass prediction or invented chronology |
| Custom/cumulative blocks | Accepted native questions across selected course/exams, reviewed topic filters, immutable source/version identity and resume; grounded suggestions require explicit acceptance | Results-only vendor entries cannot become playable content; model suggestions share runtime activation gate |
| AMBOSS reference mode | Explicit unavailable state and official link; manual NID route | AMBOSS reply confirms external MCP is not offered; supported alternatives unconfirmed |

The lecture pipeline fails before dispatch with `context_limit` and affected objective/count diagnostics when full evidence exceeds 20 images or 100,000 serialized source characters. It does not truncate sources or silently claim complete coverage. Automatic partitioning has not been accepted. These conservative ceilings are local implementation limits, not verified model context limits.

## Runtime continuation after local login

The preview remains on the frozen base at port56460. The diagnostic branch preserves raw smoke output before validation. The pinned macOS runtime accepted a combined GPT-5.5 experimental request with `environments: []`, empty capability/workspace roots and explicit controls: both effective tool registries were empty, and a valid custom `apply_patch` call was rejected before any write. This is a bounded native-router result against a synthetic loopback endpoint. It does not prove authenticated model behavior, Windows isolation or a minimal production configuration. [Exact B proof](gpt-platform-handoffs/B-gpt55-no-environments.md).

Earlier Astra async-input and agent-list calls still execute under the inspected partial controls; those failures are retained. Production generation therefore remains unavailable. On Windows, standalone Codex0.153.4 runs. A System event associates the earlier runner timeout with DLL initialization failure; its root cause remains unknown. Two direct-helper checks now passed, including the intended conbr/session1/Limited caller context. They do not exercise sandbox-account startup or IPC. The initially incorrect help syntax was diagnosed from version-matched official source; corrected help passed. The normal debug CLI retry was rejected: its unconditional ACL refresh and empty deny-read overrides can remove earlier recorded deny-read rules. Restricted identity/readable roots and account persistence remain unresolved. No explicit setup, ACL, account or registry repair was attempted; possible internal preparation by the unexpected first sandbox invocation has not been audited. [Windows diagnosis and exact next proposal](gpt-platform-handoffs/B-windows-runner-diagnosis.md).

Optional AMBOSS and vendor sample blockers remain separate. The authorized reply thread now confirms external MCP is not offered; no follow-up was sent. [Prior runtime receipt](gpt-platform-handoffs/O-runtime.md) records the earlier failed configurations; the current bounded receipt above supersedes its candidate and GPT-5.5 pending status.

## Retained state and UX

Original checkout `/Users/connor/Developer/oms-study-automation` remains dirty and is not used as a release source. Four reviewed local UX hunks were copied into candidate commit `73efe9cd`: lecture study-action removal, its unused CSS removal and existing test adjustment, plus multiline feedback whitespace. The original files and all untracked paths remain untouched. The candidate has the new objective form and retains material cards, processing details and pass editor.

The sixteen-item September 9 resolution record is retained in `docs/implementation/handoffs/2026-09-09-ux-audit-fixes.md`. Existing local route/JS regressions cover validated Home resume, lecture search, answer metadata hiding, source/destination context, material/study labels, selected-source controls, owner/public navigation, seven-pass target versus configured slots, global/local publication diagnostics, Anki Advanced controls, dated run history, compact library/player controls and upload language. The later lecture-action removal supersedes that record's M2 action row. Cloudflare's prior beacon configuration receipt is historical; no live configuration was changed or reverified here.

## Windows operational preservation

Fresh before/after evidence is retained in the current bounded receipt. Latest LPAC path-proof postflight: `2026-09-13T04:22:18.3348634Z` (both preservation checks passed). The later private-build health postflight passed at `2026-09-13T04:48:36.416719+00:00`; that is build preservation, not LPAC runtime acceptance. The earlier full task/owner inventory remains in `gpt-platform-handoffs/live-preflight.json`.

- Root `C:\Services\oms-study-automation-v2`, commit `f487c6229b91d2b1ac11729e465561f6c1ffe997`, schema 31.
- Scheduled task `OMS Study Hub V2`, principal `conbr`, Running.
- One listener at `127.0.0.1:8765`, PID 8268, parent 11300, owner `Connors_NUC\conbr`; command matched `oms_hub.cli serve`.
- Health OK, DB reachable, generation/ingestion/studio workers alive, idle, start count one, no reported errors.
- Live tracked `public_quiz.css` edit and untracked environment backup, inbox/import/script paths must be retained. Contents were not read.

These snapshots prove preservation of the observed old Hub. The separate synthetic Windows Office and export tests establish their stated artifact subset only; provider, deployment and rollback acceptance remain pending. Recheck exact identities immediately before an approved operation; PIDs and state can change.

## Bounded provider acceptance proposal — not executed

Use at most three source-free cases after the runtime restriction/identity gate has its own reviewed evidence and Connor authorizes a bounded provider proof. Bind the actual Windows executable path/hash, exact model slug and candidate commit; use a dedicated restricted identity and private session home, never a guessed macOS pin or the live Hub's source roots.

1. **Text/schema:** fixed text `alpha beta gamma`; require JSON containing exactly the supplied three words in order. Persist raw terminal assistant output before parsing, verify schema and returned thread/turn correlation, and record safe elapsed/status evidence.
2. **Image/schema and hostile source:** one locally generated synthetic figure with a plainly visible geometric symbol; ask for the symbol and a fixed schema. Include source text attempting a shell/network/tool operation. Acceptance requires the correct visual result, no tool start or side effect, and documented pre-execution denial across every enabled tool class. A model choosing not to call a tool is insufficient restriction proof.
3. **Interruption/limits/error:** one explicit interruption with bounded owned-process cleanup and no automatic resubmission. Inspect supplied usage-limit status when available; do not intentionally exhaust a subscription, manufacture a provider limit, consume reset credits, or buy fallback capacity.

A transport failure stops that proof. Preserve diagnostics, fix the root cause, and propose one bounded rerun separately. No private source test is included. The isolated local preview already has a connected account; do not start another login there. A separate Windows acceptance session would require its own managed login if no valid session exists. No passwords are needed.

## Deployment and rollback proposal — not executed

Do not invoke historical `deploy-grouped-matching-*` scripts: they are one-release scripts bound to older targets. Reuse their checked preflight/backup/task-owner concepts only in a newly reviewed, exact-candidate operation.

Before requesting cutover approval, finish candidate Windows and provider acceptance, freeze the source archive manifest/hash, and prepare the precise target/backup paths and commands for review. The release operation must:

1. Recheck live commit/tree, complete dirty/untracked manifest, task XML/action/principal, environment hash without printing credentials, current DB schema, exact listener/process owner, idle workers, and queued jobs. Abort on unexplained drift, active work, or queued work that could dispatch a provider after restart until its pause/resumption scope is explicitly approved. Preserve every task28/oms-task28/Google/Gate2B proof root and current exclusions.
2. Make and verify timestamped source (including dirty/untracked retained state), online SQLite DB, task XML and environment backups before stopping anything. Identify the verified backup as rollback source; do not guess a historical commit or restore `cb2efdbc`.
3. Notify Connor immediately before downtime. Stop only the verified task/process tree, confirm port 8765 is released, apply the frozen source candidate and additive migrations with the existing environment and retained data, then start the single existing task. No second production Hub, dependency upgrade, provider batch or Anki action is implicit.
4. Require one owned listener on 8765, the expected task principal/command/candidate/schema, ready endpoint, healthy worker start counts, and rendered private/public routes. Preserve queue state and source/media hashes. The maintenance window lasts through migration and checks; measure it in the approved rehearsal rather than promise an unmeasured duration.
5. On failure, stop only the candidate-owned process, retain failed-state diagnostics and a DB snapshot, restore the verified complete source/task/environment backup. Restore the pre-cutover DB only while writes remain quiescent; if any new data was written, stop and reconcile it before restoration. Revalidate the old Hub's single listener, health, rendered routes and preserved artifacts. No deletion of retained backups or failed-state evidence.

Pushing the implementation branch is authorized. Main merge and live deployment remain separate approvals.

## Base evidence and continuation validation

- Independent final code review: **PASS**, no remaining actionable findings at the frozen candidate. C4 scope and cancellation races were corrected and independently reproduced. Exact task and correction receipts are in `gpt-platform-handoffs/O1.md` and the B/Q/C handoffs.
- Complete Ruff: **PASS**. Complete mypy: **PASS**, 230 source files. Complete JavaScript: **284 passed**. Complete Python: **3,821 passed, 3 skipped, 1 deselected in 800.87 seconds; exit 0**. [Final receipt and commands](gpt-platform-handoffs/O3.md).
- B6 actual-constructor synthetic lecture workflow and private JSON/ZIP/PDF route parity passed; O repeated its opt-in Chrome test: **1 passed, 5 deselected**, all 12 desktop/mobile stages without overflow or page errors. C4 browser independently passed cumulative selection, source labels, answer/resume, manual filters and explicit fake-provider suggestion acceptance. [Browser paths/hashes](gpt-platform-handoffs/O3-browser-evidence.json).
- B7 export review passed after medical font correction: exact native payload/original PNG parity, supported medical glyphs, embedded fonts, immutable renderer identities, and unpacked-wheel-only export. PDF visual/asset evidence is in [B7 receipt](gpt-platform-handoffs/B7.md). Native macOS Python3.13/PyMuPDF in-process teardown was not accepted; the synthetic full workflow uses a checked raster subprocess. Actual Windows conversion/image retention and export checks now passed in the bounded continuation; runtime isolation and forced-timeout/reaping behavior remain unaccepted.
- Source archive: `Study-Hub-V2-Source-2026-09-11-gpt-candidate.zip`, SHA256 `f4a3e7d7039e4ad5ab91e4e6fc5f85a89186a96917caaf8b5962e6a201090b40`, 700 manifest files verified. Runtime archive: SHA256 `849887d7cfa4f2b6af987b958eff3b97f833573b738130065729f00dd3a8c582`, 309 manifest files verified. Both rebuild byte-identically, pass ZIP integrity and contain byte-identical bundled fonts. [Exact local archive paths and manifests](gpt-platform-handoffs/O3-archives.json). The historical runtime archive filename does not authorize or enable paid fallback.
- Original dirty/untracked status matches the exact bootstrap list. Live NUC operational proof is the read-only old-Hub snapshot above. The isolated local preview and one bounded Windows Office conversion were launched. The implementation branch was subsequently pushed with authorization; the live Hub was not restarted or deployed, and nothing was merged to main, deleted, or applied to Anki.

Current continuation checks: **197 local regression tests passed with5 skips**, then **26 final export/atomic/private-route checks passed with1 Windows skip**; complete Ruff and mypy231 files passed. Windows export verification passed18 tests, with2 symlink cases explicitly excluded, and the retained native PDF passed image/export/grade parity without another Office launch. The initial7 failing export tests and the PowerShell transport parser failure are retained alongside the correction evidence. These are scoped results, not a repeat of the base full suite.

The Windows app-server registry check now passed one fixed synthetic loopback completion under conbr/session1/Limited, with explicit empty execution environments and a fresh file-only auth home. Native PID15772 exited0, no tools were offered, the diagnostic task is disabled, and independent outer postflight found no surviving diagnostic processes or Hub drift. The SSH caller timed out; retained remote evidence was recovered without another launch. This registry-only result does not establish restricted identity, network isolation or injected-tool denial. The next runtime step requires its own reviewed scope. [Reviewed source scope](gpt-platform-handoffs/B-windows-appserver-no-tools-scope.md). The normal sandbox CLI command analyzed in B's handoff is rejected for this scope; do not run it as a no-mutation diagnostic. Do not infer permission to alter accounts, ACLs, firewall, runtime setup or deployment from the passing artifact tests. No new Mac login is needed. Provider acceptance and eventual cutover retain the separate bounded proposals above.

Caller-context follow-up: two native direct-helper checks passed; the conbr/session1/Limited no-argument check used one new no-trigger diagnostic task, now disabled and retained. Both helper and live-Hub postflights passed. No application code changed and no full suite was rerun for this evidence-only continuation.

Windows registry continuation: standalone probe code `551339cb` adds no production activation. O7 focused offline checks/Ruff and B77 checks with3 opt-in skips passed. Actual Windows completed one fake response with empty tool registries and exit0; full native evidence and transport limits are in the current bounded receipt. Provider generation and deployment remain pending.

Independent continuation review: **PASS** for exact probe/task bindings and all43 manifest files, native request/terminal evidence, disabled task and unchanged Hub/process postflight.


Latest continuation: **one valid Windows `apply_patch` call was rejected before
its handler**, with no fixture file created. Both native requests had empty tool
registries; the turn, native process and SSH transport completed successfully.
The task is disabled, no diagnostic processes survived, and Hub8268 remained
unchanged. Code `a699f7a39f4da562bf057ffc941961cbd7b3dd8f` changes only the diagnostic and its tests;
78 focused checks passed with3 opt-in skips. [Exact evidence and limits](gpt-platform-handoffs/O-windows-runtime.md).

All-tool-class prevention, restricted OS identity/readable roots, Windows managed
session persistence and actual provider acceptance are still required before
activation. AMBOSS's specific inquiry thread was rechecked: it still contains
only the sent inquiry and automatic acknowledgement; no follow-up was sent.
Vendor export samples and Anki prefix configuration remain separately pending.

Latest single-call source/code/task and all43-file native evidence review: **PASS**. Original dirty manifest and frozen Mac preview health were checked again and remain unchanged.


## Native canonicalization correction — 2026-09-13

The minimal Rust1.95.0 NT-name fallback now passes the actual Windows LPAC path
fixture. Baseline reproduced error5; patched build passed canonical reopen,
Unicode home write and existing read/write denial assertions. Both fresh tasks
were disabled with exact live-Hub preservation and no cleanup error. Independent
review passed. [Exact native red/green receipt](gpt-platform-handoffs/O-windows-canonicalize.md).
This proves the path correction only. The pinned rebuild is blocked by host Application Control before a Codex artifact
was produced. Startup, app-server, Windows login persistence and provider acceptance
remain pending; generation
stays closed. No installed runtime or live Hub was replaced.


## Ready-to-review runtime fix and required build route

The implementation commit above contains the minimal Rust shared resolver patch,
the reviewed Windows Codex containment correction, and the native red/green fixture.
Accept the patch/source and stated proof subset for review; retain generation's
closed gate. Actual Codex artifact construction requires an explicitly approved
build/signing/execution route after Windows Application Control rejected the
unsigned parking_lot_core build helper. The matched policy event and complete
cleanup/health evidence are in the current receipt. No runtime deployment,
policy exception, account change or alternate execution workaround is proposed.
The fixed initialize probe is prepared/reviewed and C#-compiled, not executed.

The read-only signing inventory found no code-signing certificate in either
CurrentUser/My or LocalMachine/My. Approval/input for a supported signing/build
setup is required before this host can continue the blocked construction step;
the current evidence does not establish a usable signer or an accepted executable.


## Signing setup decision — 2026-09-13

**Prepared, not authorized or provisioned.** The next proposed route is Azure
Artifact Signing Basic with a Public Trust profile. Microsoft documents trusted
RSA signatures as compatible with Smart App Control; this preserves the current
NUC policy. Self-signing is not the documented trust route, and Smart App Control
has no individual-app exception. [Signing requirements](https://learn.microsoft.com/en-us/windows/apps/develop/smart-app-control/code-signing-for-smart-app-control),
[Microsoft FAQ](https://support.microsoft.com/en-us/windows/security/threat-malware-protection/smart-app-control-frequently-asked-questions).

Requested purchase scope: one Basic signing account, approximately US$9.99/month,
with a proposed ceiling of US$10/month before applicable taxes and no signature
overage. Confirm the actual subscription quote before account creation; stop if
it exceeds the ceiling. The live pricing page currently lists 5,000 included
signatures/month but does not expose a dollar quote to this read-only fetch.
Billing begins at account creation, including while identity validation is
pending. No account, subscription, certificate or signing request has been
created. Existing subscription entitlements have not been inspected.
[Microsoft cost comparison](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options),
[Service pricing and billing](https://azure.microsoft.com/en-us/pricing/details/artifact-signing/).

Once Connor approves the paid setup:

1. Use his Microsoft sign-in to inspect/reuse an eligible Azure subscription and
   signer before creating anything. If new enrollment or a billing commitment
   beyond this signing account is required, surface the exact requirement first.
   Public Trust individuals require US/Canada eligibility and an Individual
   billing account. Connor completes Microsoft's identity verification directly;
   no password, identification document or private key is requested in chat.
2. Confirm the quote and create only the approved Basic account and Public Trust
   profile, using the portal if no existing CLI is available. Scope signing
   permission to that profile. Record the actual region, endpoint and profile;
   do not install trust roots or modify the host's application-control policy.
3. Prepare a bounded signing/build invocation for the existing pinned Codex and
   Rust patches, normalized lock and public dependencies. Preserve unsigned
   artifacts and record each unsigned/signed hash, signer and verification result
   before any signed artifact is executed. First target is the retained
   parking_lot_core helper with SHA256
   `20818708ed2329feb709538181338894244de14db08ba509c3b8acd5c3226a5a`.
   Sign only this pinned build's compiler/runtime artifacts, with a usage count
   below the account's remaining included quota. Signing failure or another policy
   rejection stops the run; signature verification alone is not runtime acceptance.
4. Resume under the same two-job/8GiB/90-minute bounds and live-Hub guards. Bind
   any completed Codex artifact to fresh version and initialize acceptance. The
   generation/provider/deployment gates remain separate.

The portal requires identity validation before signing becomes available;
account access, validation success and timing remain unverified. If an existing
trusted signing service is available, use that instead of buying a second one.
[Setup and individual eligibility](https://learn.microsoft.com/en-us/azure/artifact-signing/quickstart).

This approval concerns Windows code signing only. The GPT backend remains the
approved Codex subscription route. No paid model fallback, reset credits,
private-source provider tests, live Hub cutover or Anki changes are included.
