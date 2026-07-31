# NotebookLM Studio acceptance record

Date: 2026-07-31  
Branch: `codex/notebooklm-studio-main-hardening`

## Local gate

| Check | Result | Evidence |
|---|---|---|
| Full Python suite | Pass | All retained and new tests |
| Ruff | Pass | `src` and `tests` |
| mypy strict | Pass | All source modules |
| JavaScript contract suite | Pass | All Node tests |
| Fresh schema and v6/v9 upgrades | Pass | Migration and backfill tests |
| Professor URL source → chat → destination quiz | Pass | Fake-gateway end-to-end test |
| Explicit zero-source chat run | Pass | Gateway and worker tests assert `source_ids=[]` |
| Malformed/extra/zero-question responses | Pass | Bounded retry and retained raw-response tests |
| Public answer-leak regression | Pass | Studio and lecture quiz page/content tests |
| Conversion timeout/recovery | Pass (automated) | Kill, cleanup, lock-release test |
| Encrypted NotebookLM storage | Pass (automated) | Encryption, migration, cleanup tests |
| Real PowerPoint COM and live NotebookLM | Pending NUC | Requires Windows, PowerPoint, and the owner Google session |
| Manual private-page pass | Pending NUC | Follow `notebooklm-studio-rollout.md` |

The local environment is macOS, so the real Windows/PowerPoint and live Google checks are intentionally not recorded as passed.
