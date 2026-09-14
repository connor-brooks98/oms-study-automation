# O — Lecture panels and quality-of-life acceptance, 2026-09-14

Reviewed source commit: `1aadfe4fa99638045e2b2fb22c174395b6bb75ff`.
Branch: `codex/hub-intake-ux-2026-09-14`.
Base before this change: `a0f1e2166358db3d182198bc30ae8c3936cc32d8`.
Worktree: `/Users/connor/Developer/worktrees/oms-hub-intake-ux`.

## Delivered behavior

Lecture Materials and Transcript use compact matching cards, with Lecture Quiz
in a full-width workspace below. Mobile stacks the source cards. Missing files
link to unified intake; existing outlines and separate processing/pass controls
remain. Prior intake, quiz instructions, naming, export and NotebookLM upload-only
behavior are retained.

The three approved improvements are implemented:

- Saved teacher instructions: owner-scoped named presets, explicit Apply, Save and
  Delete. Selecting a preset does not replace typed text; saving does not generate
  a quiz. Limits are 30 presets per owner, 80-character names and 4,000-character
  instructions. An additive schema 41 migration creates the preset table with
  owner/name uniqueness; existing schema checks remain fail-closed.
- Missed-or-guessed review: personal quiz sessions can mark an answer as guessed.
  The latest committed answer for an exact question version determines eligibility;
  a later correct, unguessed answer clears it. Course/exam/count filters reuse the
  existing pinned study-block path with current publication eligibility checks.
- Lecture sources beside explanations: graded personal answers can expand cited
  text and open/download the original source. Page/slide locations are readable;
  native PDF links include the page. This is an excerpt preview, not generated
  image thumbnails. Stale, withdrawn, unmapped or unavailable sources fail closed.

Anonymous shared quizzes retain their public behavior. Local users can enter a
personal session using Study with progress & sources; shared-host pages do not
expose this private navigation. Source access requires ownership, an actual graded
receipt, current publication identity and trusted current source files.

## Verification and review

- Integrated Python selection: 168 passed in 59.93 seconds.
- Migration suites: 23 passed in 15.07 seconds.
- Integrated JavaScript selection: 63 passed.
- Final affected reruns: 11 source tests, 9 queue tests and 49 player/source
  JavaScript checks passed after location/copy/visual adjustments.
- Ruff and configured strict mypy passed for 10 changed source files;
  git diff whitespace check passed.
- Independent cross-reviews passed. A review found that local public quiz pages
  lacked the personal-session navigation because public routes do not populate
  private owner state. The existing owner-navigation helper now handles it;
  actual-app tests cover local visibility, shared-host absence and private access.
- Visual detector returned no findings using a degraded regex fallback; it did
  not assess full contrast or accessibility. Desktop and mobile UI were inspected.

## Actual browser proof and limits

Preview: http://127.0.0.1:60431/lectures/1
Private synthetic root:
`/Users/connor/Library/Application Support/OMSStudyHub/intake-ux-preview-20260914`.

The browser saved a teacher preset, reloaded it and explicitly applied its text.
A synthetic graded guessed answer displayed the cited red excerpt beside its
explanation and appeared in the queue. Starting a one-question review and
answering correctly without guessing cleared the queue. Source panels stack
below explanations on mobile; the lecture layout was inspected at desktop,
390-pixel mobile and the user's normal panel width.

Synthetic fixture receipt: `qol-source-receipt.json` under the preview root.
Source session: `278aa6fe-f671-43ec-b45e-8415e428b35e`.
Review session: `755aef4e-9269-4587-a568-a561ceab2880`.
One fake transport turn seeded this fixture; actual provider calls: zero.
The preview reports this source commit and schema 41. Its background workers are
intentionally disabled: `/health` is 503, `worker_not_started`, database reachable.
It is a UI review preview, not a production-ready service.

No Windows runtime, real provider, live Hub deployment, live Anki or private-source
provider acceptance was performed for this change. Earlier runtime evidence remains
separate. Original dirty work and retained resources were not modified. AMBOSS
remains deferred; optional vendor samples do not block this local candidate.

## Local verification logs

These paths are local evidence, not bundled release artifacts.

- `/tmp/hub-qol-integration.log`: SHA256 `e85d55098117c5b0628a0366fe72aa38aae23b726af50d922d3bbd2f8b4ba610`
- `/tmp/hub-qol-migration.log`: SHA256 `7b3cf1485a4f4d13c010cfe2e2987bdad50730beaed4690d4f8728f0a2967fe3`
- `/tmp/hub-qol-node.log`: SHA256 `582dd85193c0eabbb142d004ab7f8aa3bd37a64dc44fa58c9c706ae529ebe514`
- `/tmp/hub-qol-source-final.log`: SHA256 `fb47dcc78ffe5873af8855794114a8d9ac47a6efccb478ddcf811c67ee804e5d`
- `/tmp/hub-qol-source-node-final.log`: SHA256 `5a7079f310bee475ce31d9b78db16aaa0c3b4d233b5167436e8f76a744521183`
- `/tmp/hub-qol-queue-final.log`: SHA256 `7eb77823d27e40192865ca0d06917d4589aec606be48326c120e8eb42a21043e`
