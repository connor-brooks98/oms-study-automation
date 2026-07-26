# Study Hub V2 acceptance record

Date: 2026-07-24  
Branch: `v2-manual-ingestion`  
Production credentials or transcript contents: not recorded

## Local implementation gate

| Check | Result | Evidence |
|---|---|---|
| Retained automated tests | Pass | 103 tests |
| Ruff | Pass | Entire `src` and `tests` trees |
| mypy strict mode | Pass | 44 source files |
| Tracker → upload → quarantine → processing → replacement workflow | Pass | Representative V2 acceptance test |
| Interrupted-job recovery | Pass | Recovery, bounded retry, permanent-failure tests |
| Cloudflare JWT and CSRF controls | Pass | Signed-token, identity, expiry, CSRF, and header tests |
| Legacy acquisition removal | Pass | No connector modules/routes/tables in a fresh database |
| Local server smoke test | Pass | Health 200; dashboard 200 with V2 navigation and no connector panels |
| Desktop/mobile interface | Pass | Dashboard, upload, quarantine, review, lecture, and Anki pages checked during implementation |
| Windows PowerShell execution | Pending | Requires the Windows NUC |
| Live Cloudflare Access/Tunnel | Pending | Requires the production hostname and Zero Trust account |
| Real PowerPoint COM conversion | Pending | Requires interactive Microsoft PowerPoint on the NUC |
| Real iCloud replication | Pending | Requires iCloud for Windows on the NUC |

## NUC acceptance

Record only pass/fail, timestamps, checksums, and non-sensitive paths.

| Scenario | Status | Notes |
|---|---|---|
| Timestamped database and artifact backup | Not run | |
| `install-windows.ps1 -WhatIf` | Not run | |
| Side-by-side V2 install and migration | Not run | |
| Local health and dashboard | Not run | |
| Tracker preview/apply | Not run | |
| Confident PPTX match and PDF conversion | Not run | |
| Ambiguous PPTX quarantine and assignment | Not run | |
| Confident TXT match and cleaning | Not run | |
| Ambiguous TXT quarantine and assignment | Not run | |
| Duplicate detection | Not run | |
| Replacement approve/keep | Not run | |
| Restart recovery | Not run | |
| NUC and iCloud artifact destinations | Not run | |
| Cloudflare missing/invalid identity rejection | Not run | |
| Cloudflare allowed-email access | Not run | |
| Remote phone upload and artifact open | Not run | |
| NUC-off remote behavior | Not run | Expected unavailable |
| One complete real lecture cycle | Not run | Required before retiring old deployment |

## Promotion decision

Status: **Not yet promoted**

Promotion requires every NUC acceptance row to pass. If any row fails, stop V2,
restore the prior scheduled task, and preserve both the old deployment and the
timestamped V2 backup unchanged.
