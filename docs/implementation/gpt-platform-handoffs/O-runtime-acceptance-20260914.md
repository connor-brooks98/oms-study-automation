# O: Windows restrictions and Mac generation acceptance — 2026-09-14

User scope: continue bounded Windows restriction checks; launch a separate Mac Hub
and test synthetic GPT generation using the existing ChatGPT subscription. No private
lecture/provider test, paid fallback, reset credit, live Hub deployment or Anki change.
AMBOSS remains deferred. Vendor export samples remain separate from core acceptance.

Current outcome: the requested bounded Windows diagnostics and Mac synthetic Hub
workflow passed independent review. The Mac preview is running at
http://127.0.0.1:62170, PID19725, on application commit
`ab21eff5e98eec15d7e80f2f4596a06341db192d`. Windows production launcher integration,
Windows account/provider acceptance and live deployment remain separate.

## Mac preview and runtime drift

New isolated preview: http://127.0.0.1:62170/settings, PID 6267, source checkpoint
`c213a871f17d6a0b4dc31bbca5a816ac236bbe5b`, tree
`a2f15d3feffac37f0e679d6963056d1c6e326236`. Its empty schema-40 database and lecture
root are separate; Anki is disabled. The previous port-56460 preview remains healthy
and unchanged. The managed auth file was copied privately (directory 700/file 600)
without displaying credentials; new account validity has not yet been checked.

Fresh preflight rejected the previously pinned Mac binary: the same path now hashes
`ecad78dbf98adb89ec475edac86630406cbe59d9f3070b17d88065f136b94bcb`, not accepted
`87a08119b8effa519f0ecb552dc98043f58a8200bf2ec5da60f76890c33e9c3a`.
The new UI correctly remains capability-unverified; production pins were not changed.
An independently reviewed auth-free, network-denied inspection identifies the current
signed artifact as `codex-cli 0.154.0-alpha.6.2`, with 103 feature keys rather than 99.
Selected protocol comparison covers 90 referenced definitions; current Thread adds
optional fields and ThreadEnvironment. Prior native policy acceptance does not transfer.

The narrow Mac file profile initially prevented even the allowed test read. A literal
root-directory read permission fixed loader startup; it does not grant recursive root
access. All four synthetic allowed/denied read/write canaries then passed. Inspection
and later fake-provider fixtures retain separate evidence from any real GPT request.
The first current fake-provider preparation stopped before app-server launch because
features-list does not accept the app-server-only strict-config argument. The reviewed
correction removes that flag only from the metadata invocation; actual app-server
keeps strict config and the complete fixed policy.

The third current-runtime fake-provider run passed independent review: two requests
offered no tools, the injected apply_patch call received its exact correlated
unsupported-tool response, and the canary remained absent. The four filesystem
canaries passed; the native process exited 0 and was reaped. Result SHA256:
`3f5bdbe747a16d801db05fe05948cf0fe010acadbdd51073451f2dc475b7bc22`.
The second attempt stopped before app-server startup on effective feature drift:
the client normalizes unified_exec=true despite a requested false. The diagnostic
now explicitly expects 102 false features plus unified_exec=true, supported by the
inspected native source and observation. This does not establish tool availability;
the actual empty registry and rejected injected call are the scoped tool evidence.

The real synthetic provider test uses a private frozen copy of the current binary,
the new preview's dedicated session, and one fixed text/image/schema turn. Native
internal HTTP retries cannot be disabled through the inspected built-in provider
overrides. One turn/start is permitted; reported limits or retry errors stop the
diagnostic, with no application resubmission or paid fallback. Account evidence is
limited to connection/auth type and usage state, with no account frames or tokens.

Actual Mac subscription diagnostic passed: ChatGPT account connected, no reported
limit, one GPT-5.5 turn returned `{"number":7,"color":"red"}`. Raw synthetic output
was retained before validation (SHA256
`32c2bdc0d7113affe8c04217a1d6a334c8345ceeb7827c5748032623e3fc2f73`).
Result SHA256: `320f3832ef8df3c8aae910d71b9f785df51378b016378072ad8d5abb459c63c4`.
All four filesystem canaries passed. No retry/error notification occurred. The
completed turn was validated before the shared close method terminated and reaped
the idle app-server (exit -15). This proves the synthetic provider modality/schema
subset; it does not prove full restrictions, the Hub workflow or deployment.

Two later auth-free native checks passed. The matrix returned all15 exact correlated
unsupported-call results with empty tool registries, no action events, unchanged
canaries and normal exit0 (result
`615454e0a8c7de33216a664c200e6a86b682523b5981c924a4f8c8362f8b1582`).
The controlled interruption withheld the provider response body, received the native
interrupt acknowledgement and matching interrupted completion, and exited0 with
empty stderr (result
`76918c0dfd0baef627d28683e39f193906f033b085268a604c34cd08a4343524`).
Both removed their owned temporary work and passed all four file canaries. Forced
calls establish native rejection before handler dispatch; a fake provider does not
establish model semantic obedience. Shared production integration remains separate.

The Mac production integration is now committed and pushed as
`ab21eff5e98eec15d7e80f2f4596a06341db192d`, tree
`dfcb17d27dc012486202bdf78472a8ea795c45f2`. It reuses the accepted policy/profile,
pins the current artifact/version, confines each fresh request process to its own
staging directory plus private session, and requires fresh ChatGPT authentication
and usage checks before dispatch. Windows generation stays closed. Independent
source review,93 focused tests (3 opt-in skips), Ruff and scoped strict mypy passed.
The new preview alone was restarted at62170, PID16907. Actual browser status showed
account connected, generation ready and GPT-5.5 image support; the model was saved.
The old56460 preview remains healthy and unchanged.

Evidence limit: exact matrix/interrupt diagnostic source was preserved by hash in
source-snapshots. The earlier provider diagnostic's hash and actual receipts were
preserved, but its complete exact source snapshot was not retained before later
extensions. Do not describe that historical provider bundle as a complete source
archive. Current production source is committed and independently reviewed.

## Windows precursor and corrected interpretation

The accepted private executable remains SHA256
`d6eb90b7409dc22f407a9dfa44ec629a8a5a6bf3ca001493aef13dd85a75dbda`.
Fresh NUC preflight preserved the exact live Hub (PID 8268, schema 31, revision
`f487c6229b91d2b1ac11729e465561f6c1ffe997`) and worker identities/start counts.

Both strict LPAC thread-start precursors returned the expected policy echo, but their
full wrappers failed. V1 rejected optional emittedAtMs notification metadata and
reported private-home GLOBALROOT skill-scan errors. V2 accepted that metadata but
rejected an added feature warning; skill-scan errors remained. Both owned children
were reaped without timeout, tasks disabled, and independent Hub preservation passed.
Neither failed run is relabeled as accepted.

The attempted shared skip_host_skill_discovery=true correction was ineffective:
app-server installs a HostSkillProvider unconditionally, which requests discovery
despite that flag. The production flag/test/fingerprint changes were reverted. Their
local test results do not establish native scan prevention. No Rust rebuild is needed
merely to silence an unused private-home scan diagnostic.

Ruling: continue with explicit adversarial tool/read assertions. Precisely identified,
nonfatal private-home metadata diagnostics stay visible and separate from those
assertions; unknown/fatal output still fails. No third thread-only rerun is planned.
A fresh LPAC capability for the host client's fixed synthetic LAN provider is proposed
separately; it is not permission for model-driven networking or a shared firewall change.

The first reviewed adversarial turn timed out at the fixed 45-second child deadline.
LPAC identity, initialize and thread policy checks passed; turn/start and user-message
events arrived, but the owned Mac fixture received zero HTTP requests. No injected
tool reached the client, so this is not a tool-denial result. Child16244 was killed
and reaped, the task disabled, and independent preservation at18:44:05Z found no
owned processes or live-Hub drift. Both fixture and native diagnostics are stopped.
Next diagnosis separates network reachability from client startup progress.

The separate connectivity discriminator stopped at its ordinary Limited control:
curl exited28 after5003ms with no bytes received. It never created an LPAC profile
or child. All74 frozen files and cleanup/preservation passed independent review
(completion manifest `26adb85814acc346a6768f5a7db0cf727dfeaefa8c33a7cf27bec18e15c94ece`).
This failure cannot establish LPAC network denial. Read-only macOS Application
Firewall logs identify the exact fixture PID13021 and report a pending inbound
decision for Homebrew Python at the control's start time. The proposed correction
uses the already-allowed system Python for the synthetic fixture; it changes no
firewall setting, endpoint policy or Windows runtime.

The correction passed: ordinary Limited PID25592 and LPAC PID13360 both exited0
with the exact HTTP marker and empty stderr. The fixture received exactly two
ordered nonce GETs. LPAC identity/capabilities/access checks, native cleanup, task
disablement and exact live-Hub preservation passed. Independent review verified
all77 files against completion manifest
`517bee62d0d9787c2304905dd0c1e31245c87962a855e9da63d18cfc2080d941`.
This accepts the permitted generic HTTP route, not endpoint confinement or Codex
tooling. A fresh Codex matrix reuses this accepted interpreter without another
connectivity test or runtime rebuild.

The corrected Windows Codex matrix passed: all15 exact correlated tool-call
rejections, empty registry, matching completed turn and child25424 normal exit0.
The77-file completion manifest is
`6321e876a4b333d10d9b5b85e4815a0ee6fcb19b7e2852c060fd91504170a529`.
Current three-capability file-access and controlled interruption checks remain
separate; the matrix does not activate the production Windows launcher or account.

The current three-capability OS canary run subsequently passed independent review:
staged read/private-home write succeeded; outside read and writes to both read-only
roots were denied. Exact positive output and write contents were retained, forbidden
paths absent and markers unchanged. Cmd25144 exited the expected1 after its final
denied operation and was reaped without timeout/kill. All61 files match manifest
`d8393a687383c344ef84b19ab580e537405311fdac4a70204baffb29dea6c68b`.
Task disablement, owned-process absence and exact live-Hub preservation passed.

Final controlled interruption passed independent review. The fake request arrived
2.109s before the single observed-ID interrupt; no response body was sent. Exact
empty acknowledgement and matching interrupted terminal with empty items arrived;
child5868 exited0 and was reaped. Current LPAC checks, cleanup, task disablement
and live-Hub preservation passed. All79 files match
`b6dbb0532d100175a04fb737ae9482004d71826ffab82aefba1c2634d8d2c08a`.
The bounded Windows diagnostics are complete. Production Windows launcher
integration, managed account/provider acceptance and deployment remain separate.

All Windows evidence, including failed attempts, is retained in
`../gpt-platform-evidence/windows-runtime-restrictions-20260914.zip`, SHA256
`dd2a039f5fc6778e834682c59905945b2c4b76010a6cec8e1511a9e2a57889be`.
The archive contains586 entries; all585 payload files match the closed inventory,
and ZIP integrity passed. No executable/runtime binaries or credentials are included.

## Evidence and next action

Local evidence under the task visualization root:

- `windows-restrictions-20260914/thread-start`: failed v1, 55 completion hashes,
  manifest `52c57f3cf153e4488a469278b189317a895126f1156365f2e52bf8c1768216cb`.
- `windows-restrictions-20260914/thread-start-v2`: failed v2, 55 completion hashes,
  manifest `9debd4dac843723e6bb0ccb1f31afa515750af78e8e0c9f4ddbee5a65ee80082`.
- `mac-generation-acceptance-20260914/current-ecad-inspection-1`: actual current
  binary version/features/schema evidence, no auth/provider.
- `mac-generation-acceptance-20260914/current-ecad-fixture-1`: metadata-command
  failure before app-server/provider startup.

## Completed Mac Hub workflow

The first two attempts stopped before any provider dispatch: the fresh preview
needed its local staging directory and the approved synthetic transcript-prompt
hash. Both failures and uploads were retained. Supported quarantine reassignment
resumed the same logical items after those configuration corrections; no duplicate
lecture, slide upload or provider retry was needed.

The final attempt completed exactly one transcript-cleaning turn and one quiz turn,
with distinct completed thread/turn identities. The cleaned222-character transcript
equals the source. Three questions cover both supplied objectives; reviewed answer
indices are0,2,1, and all answer/distractor explanations match the source. The first
question retains the original500x240 diagram. Independent review checked source
grounding, the generation journals and the actual image before answer verification.

The quiz was verified and published once in the isolated preview. Public answer
grading, image parity and JSON/ZIP/PDF exports passed. O also inspected the actual
rendered browser quiz and diagram. User-facing result:
http://127.0.0.1:62170/public/quizzes/559c1c89b5e9afd17b418b0c311c69e12d5b75b7d22893953c34a461af5a0b66

Limits: this preview accepts only the exact synthetic PowerPoint fixture through
the existing test converter; native macOS Office conversion is unverified. The
production Codex client and subscription generation are real. Requested/effective
model checks use GPT-5.5; the quiz journal's `actual_model=null` and
`model_evidence=unverified` remain unchanged. No provider calls followed generation.

Mac evidence archive: `../gpt-platform-evidence/macos-runtime-and-hub-acceptance-20260914.zip`,
SHA256 `b0c3ecaa8ffff00258b21872f9fc644d8ef4905f66746edc1a4d2f026ff057f3`.
Independent review verified all828 payload files plus inventory, ZIP integrity,
final publication/grading and export parity. It includes failed attempts and safe
launcher receipts, but no managed auth files. The earlier provider-source snapshot
gap noted above remains explicit.

Next: integrate the accepted Windows restrictions into its production launcher,
then obtain separately scoped Windows account/provider acceptance and prepare the
final deployment package/rollback review. AMBOSS stays deferred; vendor adapters
need exact supported samples. The live Hub and Anki remain unchanged.
