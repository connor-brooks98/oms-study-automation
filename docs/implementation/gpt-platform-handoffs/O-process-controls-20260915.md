# O: processes, material removal, and upload navigation — 2026-09-15

Candidate branch: `codex/processes-and-material-controls-2026-09-15`, based on
`7abf6fdd9683986663b4719048e95d0fc9883a16` in the existing isolated intake worktree.

The current Cardio Lecture 1 transcript (lecture 110, revision 250) is complete.
The older duplicate upload failed with an unavailable-model error; there is no
proposed replacement to approve. Actual proposed replacements now link from the
lecture page directly to their review card. Processes makes older failures visible
separately from current material readiness.

`/processes` projects existing upload, generation, Studio/import, source-sync, Anki,
and question-bank records. Owner/CSRF-protected controls persist pause/removal holds
in one new schema-42 table; workers acknowledge them at safe boundaries. Existing
GPT cancellation/resume journals remain authoritative. Remote source reconciliation
and active Anki applies cannot be interrupted here. Interrupted paid work without a
safe checkpoint stays in its existing review flow. Remove hides/stops an event;
files and historical records remain retained. Completed history is limited to the
latest 100 events; unfinished/review work remains visible and searchable.

Lecture materials/transcripts can be detached and restored from their lecture page.
Removal keeps original/derived bytes, revision identity, catalog, quizzes, and Anki
scheduling. Active writers must stop first. Restore reconnects intact retained and
filed copies and never overwrites a newer current revision. An exact-file reupload
uses Restore; a changed source creates its own revision without requiring replacement
approval when no current material remains. Missing or changed filed copies need
recovery; this UI does not silently rewrite historical artifacts.

The blue Upload Materials button is between Practice Questions and More. Navigation
switches to the mobile menu before the added button crowds smaller desktop widths.

Local evidence: 301 JavaScript tests passed. A fresh copy of the live schema-41 DB
migrated to 42 twice with all 80 existing tables unchanged except schema_version;
integrity and foreign-key checks passed. Browser fixture checks verified removal,
restoration, failed-event dismissal, and desktop/mobile navigation. Independent review passed after closing revision-lineage, optional-source callback,
ambiguous paid replay, and GPT pause/resume interleaving findings. Final worker/material
regression counts are recorded with the release receipt.

Release evidence: `/Users/connor/Developer/release-evidence/process-controls-20260915`.
Local fixture tests and migration rehearsal do not establish deployed behavior or
new Windows/provider generation acceptance. Managed login and Windows generation
restrictions remain unchanged. No private provider turn or live Anki mutation is
part of this change.
