# GPT Study Hub candidate — release proposal

Status: implementation and local acceptance in progress. No deployment is authorized or performed. The final commit, check results and export hashes will be frozen below after B7/C4 integration and independent review.

## Intended release and limits

This replaces the required Google path for new lecture quizzes with an explicitly selected Codex subscription backend, preserves full slide/transcript evidence and image assets, requires question/answer review before native publication, and adds private source-scoped/general chat. Outlines are optional. Existing publications, imported artifacts, Anki scheduling and Gate 2B proof resources are retained.

**Activation is blocked, even after local tests pass.** The inspected Codex runtime has not established universal pre-execution tool prevention, restricted Windows identity/readable roots, account persistence, or actual model text/image/schema acceptance. Production generation fails closed with `capability_unverified`; no paid fallback or configuration bypass is present. Fake-client tests prove the application workflow only. The single managed-device login attempt timed out and was cancelled; no account connection was confirmed and no automatic retry is planned.

| Capability | Local candidate scope | External/operational limit |
| --- | --- | --- |
| Full lecture-image quiz | Full approved slides and cleaned transcript, frozen objective coverage, source locators, original sanitized images, native review and publication | Actual Windows Office conversion and subscription generation not accepted |
| Optional outline | Current-source validation through filing/commit; durable raw result; PDF filing with Codex provenance | GPT replacement of any retained imported NotebookLM outline is explicitly unavailable; historical legacy review is preserved |
| Native JSON/ZIP/PDF exports | Same native payload and original PNGs; source/objective manifest; embedded-font PDF with unsupported-text rejection; private download parity reviewed | No private lecture evidence used |
| Lecture/general chat | Owner-scoped current source snapshots, citations, durable history, cancellation/ambiguous-turn protection | Same runtime/account activation gate |
| Question-bank imports | Normalized results-only or explicitly authorized content, preview/confirm, identity conflicts and review | Exact supported UWorld and TrueLearn samples still needed; no guessed proprietary parsers |
| Anki candidates | Exact observed source/product-qualified QIDs and allowlisted pasted NIDs against an existing local index | Real prefix rules/index setup pending; no live Anki changes or full curation |
| Private progress | Server-issued attempt identity, durable exact replay, first/repeat and omitted/unknown counts | Descriptive counts only; no pass prediction or invented chronology |
| Custom/cumulative blocks | C4 implementation pending | Results-only vendor entries cannot become playable content |
| AMBOSS reference mode | Explicit unavailable state and official link; manual NID route | Specific sent thread has an automatic acknowledgement only; account/subscription does not prove MCP entitlement |

## Retained state and UX

Original checkout `/Users/connor/Developer/oms-study-automation` remains dirty and is not used as a release source. Four reviewed local UX hunks were copied into candidate commit `73efe9cd`: lecture study-action removal, its unused CSS removal and existing test adjustment, plus multiline feedback whitespace. The original files and all untracked paths remain untouched. The candidate has the new objective form and retains material cards, processing details and pass editor.

The sixteen-item September 9 resolution record is retained in `docs/implementation/handoffs/2026-09-09-ux-audit-fixes.md`. Existing local route/JS regressions cover validated Home resume, lecture search, answer metadata hiding, source/destination context, material/study labels, selected-source controls, owner/public navigation, seven-pass target versus configured slots, global/local publication diagnostics, Anki Advanced controls, dated run history, compact library/player controls and upload language. The later lecture-action removal supersedes that record's M2 action row. Cloudflare's prior beacon configuration receipt is historical; no live configuration was changed or reverified here.

## Read-only Windows operational snapshot

Exact receipt: `gpt-platform-handoffs/live-preflight.json`, observed `2026-09-11T23:30:15Z`.

- Root `C:\Services\oms-study-automation-v2`, commit `f487c6229b91d2b1ac11729e465561f6c1ffe997`, schema 31.
- Scheduled task `OMS Study Hub V2`, principal `conbr`, Running.
- One listener at `127.0.0.1:8765`, PID 8268, parent 11300, owner `Connors_NUC\conbr`; command matched `oms_hub.cli serve`.
- Health OK, DB reachable, generation/ingestion/studio workers alive, idle, start count one, no reported errors.
- Live tracked `public_quiz.css` edit and untracked environment backup, inbox/import/script paths must be retained. Contents were not read.

This proves only the observed running old Hub. It is not candidate Windows, provider, deployment, or rollback acceptance. Recheck exact identities immediately before an approved operation; PIDs and state can change.

## Bounded provider acceptance proposal — not executed

Use at most three source-free cases after the runtime restriction/identity gate has its own reviewed evidence and Connor authorizes a bounded provider proof. Bind the actual Windows executable path/hash, exact model slug and candidate commit; use a dedicated restricted identity and private session home, never a guessed macOS pin or the live Hub's source roots.

1. **Text/schema:** fixed text `alpha beta gamma`; require JSON containing exactly the supplied three words in order. Persist raw terminal assistant output before parsing, verify schema and returned thread/turn correlation, and record safe elapsed/status evidence.
2. **Image/schema and hostile source:** one locally generated synthetic figure with a plainly visible geometric symbol; ask for the symbol and a fixed schema. Include source text attempting a shell/network/tool operation. Acceptance requires the correct visual result, no tool start or side effect, and documented pre-execution denial across every enabled tool class. A model choosing not to call a tool is insufficient restriction proof.
3. **Interruption/limits/error:** one explicit interruption with bounded owned-process cleanup and no automatic resubmission. Inspect supplied usage-limit status when available; do not intentionally exhaust a subscription, manufacture a provider limit, consume reset credits, or buy fallback capacity.

A transport failure stops that proof. Preserve diagnostics, fix the root cause, and propose one bounded rerun separately. No private source test is included. A fresh managed login would require Connor to act on a newly issued actual link/code; the prior code is expired. No passwords are needed.

## Deployment and rollback proposal — not executed

Do not invoke historical `deploy-grouped-matching-*` scripts: they are one-release scripts bound to older targets. Reuse their checked preflight/backup/task-owner concepts only in a newly reviewed, exact-candidate operation.

Before requesting cutover approval, finish candidate Windows and provider acceptance, freeze the source archive manifest/hash, and prepare the precise target/backup paths and commands for review. The release operation must:

1. Recheck live commit/tree, complete dirty/untracked manifest, task XML/action/principal, environment hash without printing credentials, current DB schema, exact listener/process owner, idle workers, and queued jobs. Abort on unexplained drift, active work, or queued work that could dispatch a provider after restart until its pause/resumption scope is explicitly approved. Preserve every task28/oms-task28/Google/Gate2B proof root and current exclusions.
2. Make and verify timestamped source (including dirty/untracked retained state), online SQLite DB, task XML and environment backups before stopping anything. Identify the verified backup as rollback source; do not guess a historical commit or restore `cb2efdbc`.
3. Notify Connor immediately before downtime. Stop only the verified task/process tree, confirm port 8765 is released, apply the frozen source candidate and additive migrations with the existing environment and retained data, then start the single existing task. No second production Hub, dependency upgrade, provider batch or Anki action is implicit.
4. Require one owned listener on 8765, the expected task principal/command/candidate/schema, ready endpoint, healthy worker start counts, and rendered private/public routes. Preserve queue state and source/media hashes. The maintenance window lasts through migration and checks; measure it in the approved rehearsal rather than promise an unmeasured duration.
5. On failure, stop only the candidate-owned process, retain failed-state diagnostics and a DB snapshot, restore the verified complete source/task/environment backup. Restore the pre-cutover DB only while writes remain quiescent; if any new data was written, stop and reconcile it before restoration. Revalidate the old Hub's single listener, health, rendered routes and preserved artifacts. No deletion of retained backups or failed-state evidence.

Push/main merge and deployment are separate approvals. Local candidate review does not authorize either.

## Final evidence

Pending final freeze: exact code/receipt commits, complete CI-equivalent checks, independent review, desktop/mobile synthetic browser evidence, export parity/PDF visual evidence and deterministic source archive checksum.
