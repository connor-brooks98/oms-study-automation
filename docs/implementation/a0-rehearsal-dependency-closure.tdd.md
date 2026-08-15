# A0 rehearsal dependency-closure TDD evidence

Date: 2026-08-14

## User-visible contract

The A0 launcher must preserve its isolated `-I -S` base-Python child while
making the selected Windows virtual environment's physical third-party package
directory explicit and attestable. The preflight must fail before Hub source
execution when the required dependency closure is missing, malformed, or
resolves outside the launcher-attested directories. The Job-contained Hub child
must attest the same dependency origins and versions before running the goal.

The supported offline provisioning path is a reviewed Windows wheelhouse, a
separate wheel SHA-256 manifest, a fresh virtual environment, an offline install,
and verification against the tracked runtime lock. A copied virtual environment
alone is not accepted as a durable provisioning strategy.

## RED evidence

The first valid RED run contained four intended failures:

- `RehearsalRequest` did not accept `trusted_dependency_paths`.
- the full isolated launcher re-exec did not carry that field;
- source attestation did not reject dependency-closure tampering; and
- the Windows child capability contract had no trusted-path/origin evidence.

An earlier copied-interpreter fixture failed for a macOS fixture-construction
reason. Only the fixture was corrected before the valid RED run; no production
behavior was changed to bypass it.

The focused read-only review then identified two test-evidence gaps. The
follow-up tests now use existing module files outside the trusted dependency
root and exercise the runtime-lock verifier through synthetic `.dist-info`
metadata, normalized-name duplication, incomplete metadata, lock tampering, and
CLI path rejection. The missing-`Version` case produced a valid RED: accessing
`Distribution.version` raised a promoted `DeprecationWarning` before the
verifier's explicit incomplete-metadata error. The minimal GREEN correction
reads `Version` through `distribution.metadata.get`, matching the existing
`Name` handling.

## GREEN implementation evidence

- Focused process and runtime-lock suites: 100 passed.
- Selected A0 suite: 325 passed.
- Ruff: clean.
- Windows/A0 mypy scope: clean across 15 source files.
- `git diff --check`: clean.
- Runtime-lock verifier coverage: 95% (73 statements, 4 missing).
- Changed executable lines in `process.py`: 52 of 55 covered, 94.55%.
- Whole legacy `process.py`: 67% (1,523 statements, 506 missing). This is a
  pre-existing whole-module coverage gap and is not represented as passing an
  80% whole-file threshold.

The broad repository baseline completed with 2,324 passed, 2 skipped, and four
failures in the unrelated optional `anydoc` area. Full-repository mypy likewise
reported only the existing missing-stub findings for `pdf_inspector` and
`anydoc`. After the final argv-only `-B` bytecode hardening, the focused and A0
suites plus the static checks above were refreshed successfully.

## Native read-only closure confirmation

The preserved LF1 Windows environment was checked without invoking the launcher,
Hub server, package tools, or a rehearsal goal. Exactly one child was started:

```text
C:\Users\conbr\AppData\Local\Programs\Python\Python312\python.exe -I -S -B -
```

It exited 0 with 928 stdout bytes and empty stderr. Its canonical result showed:

- FastAPI 0.141.1, SQLAlchemy 2.0.52, Starlette 1.6.0, and Uvicorn 0.52.3
  resolving beneath the physical LF1 `.venv\Lib\site-packages` directory;
- `oms_hub` and `oms_hub.cli` resolving beneath the LF1 source directory;
- `_overlapped` and `asyncio` importing successfully; and
- exactly the reduced 22-key environment, including canonical `SYSTEMROOT`.

The stdout SHA-256 was
`f53df222c65194f12411103ff48f51444b2bd68ebe1c7e3927be07d77105417f`.
All source, site-packages, preserved evidence, Git, port, process, and production
before/after checks were identical. The native conclusion was:
`PHYSICAL VENV CLOSURE CONFIRMED`.

## Scope boundary

This evidence proves the corrected physical-path and dependency-closure model,
not native rehearsal acceptance. No source synchronization, launcher run,
server start, rehearsal retry, staging, commit, or push was performed for this
correction. Checkpoint commits were intentionally omitted because this work was
authorized for completion and findings review, not for Git publication.
