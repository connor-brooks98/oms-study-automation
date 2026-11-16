# Notebook cleanup deployment — October 8, 2026

Both installed applications are healthy after the user-authorized rollout.

- Hub runtime: `c30469ad250657f31cb9b6376d208edada7e7a17`, tree `9d542c6fe22bbf2577fa55118fba0df501b4904b`, schema 42, one listener and three healthy workers. The scheduled task is Running and `.env` hash is unchanged.
- Clinical Notebook runtime: `247a5b1235fc7c5090fdd48424a96a487199258a`, installed at `/Users/connor/Developer/GPTLM-Build/Clinical Notebook.app`. The combined source is `/Users/connor/Developer/clinical-notebook-deployment-20261008`. It preserves all 62 newer source files from the other session plus the reviewed API implementation; the original dirty checkout remains unchanged.
- 96 abandoned NotebookLM transfers changed from needs-review to skipped with audit history. Counts/states of other ingestion, generation, Studio and upload records stayed unchanged. The Quarantine page has one eligible Delete upload control; the mistaken file remains present.
- Old NotebookLM SDK 0.8.1 was removed from the Hub environment. Cold configuration validation against an in-memory database passes without it.
- Clinical library metadata still matches all seven notebooks, 60 sources and two notes. Owner/account/voice configurations are unchanged; deep/strict signature verification passes.

## Connection prepared; notebook choice left to the user

The Mac has supervised LaunchAgents `dev.clinical-notebook.source-gateway` and `dev.clinical-notebook.study-hub-tunnel`. They bind only loopback and use the existing SSH connection to the NUC. Tests from the NUC confirm authentication is required and native administrative routes are not exposed. The gateway follows Clinical Notebook's owner-only runtime discovery file across app launches.

1. In Clinical Notebook **Settings → Connected Apps**, create a named Study Hub connection and choose the allowed notebooks. For delivery, grant notebook listing and source import. Reading/exporting are optional separate permissions.
2. In Hub **Settings → Clinical Notebook**, enter the token and use **`http://127.0.0.1:15160/api/external/v1`**. This address is on the Hub computer's private loopback interface and reaches the Mac through SSH.
3. Choose the destination on a lecture and send its selected materials. Keep Clinical Notebook open during delivery.

The user explicitly chose to leave notebook selection to Settings. No grant, destination mapping or real source delivery was created during deployment. Duplicate notebook names now include stable ID hints so both choices are distinguishable.

## Backups and verification

Hub verified database/artifact backup: `C:\ProgramData\OMSStudyHub-V2\backups\20261008-114446` (956 manifest entries). Old source/config/task backup: `C:\ProgramData\OMSStudyHub-V2\release-rollback\notebook-cleanup-20261008-a9a859f5`. The old runtime was `0a2d6f526bd4e896bbd10044b38460f1aad9ad34`. Rollback through the normal installer restores its declared dependencies, including the old SDK if needed. Assess any subsequent user data before restoring the saved database.

The guarded installer verified the database and 2.6 GB of artifacts. Observed listener absence was **226.07 seconds**; this is a listener measurement, not an exact readiness duration. The first dry-run wrapper lost its process exit code without changing the service; immediate handle retention was reproduced and verified to preserve zero/nonzero exit codes before the actual rollout.

Native hash-verified backups and restoration steps are in [native rollback instructions](/Users/connor/Developer/release-evidence/2026-10-08-notebook-upload-cleanup-deployment/native/ROLLBACK.md). Full native deployment details are in [native deployment report](/Users/connor/Developer/release-evidence/2026-10-08-notebook-upload-cleanup-deployment/native/README.md). Original owner data is protected in `.noindex` backup folders.

Final Hub JavaScript suite: 320 passed. Combined Clinical backend: 341 passed; runtime: 20 passed; native suite: 367 reported with 36 opt-in skips and zero failures. The final duplicate-label change passed focused and independent checks. Earlier focused Hub/API acceptance and the classified whole-repository Python survey are retained in the implementation evidence; this report does not claim all preexisting repository tests pass.

The [verification record](/Users/connor/Developer/release-evidence/2026-10-08-notebook-upload-cleanup-deployment/verification.json) links the actual source identities and observations. `hub/` retains preflight/deploy/postflight records, HTTP and database checks, SDK removal, cold validation, immutable archives and their per-file hash verification. `connection/` records the loopback gateway/tunnel configuration and access checks. No live ExtendLM upload or real Clinical source transfer was performed.
