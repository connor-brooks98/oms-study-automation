# O1 shared schema increment

Base: 37f6beddec4ae53043aaae779b85e22a39072756. Implementation: 86bf3e2583614b0e52a80a998cd6f05fc67c538e; correction: e639451187bd214b464787c88c0c6f8b31a7305c.

Adds Studio backend with notebooklm legacy default and Q3 agreed bank tables, unique source/product/string QID, learner/question/attempt uniqueness, immutable import row provenance and topic review audit. Q3 set_topics requires reviewer_context str supplied by authenticated server boundary.

Independent reviewer blocked initial version: v31 upgrade replayed historical backfills and changed published content kind. Corrected with validate-v31 then new-delta-only upgrade; published quiz and media rows are compared unchanged over migration and replay.

Verification: PYTHONPATH=src /Users/connor/Developer/oms-study-automation/.venv/bin/python -m pytest tests/study_generation/test_gpt_platform_migration.py tests/study_generation/test_migration.py -q: 22 passed. Scoped Ruff and mypy models/migrations passed. Local synthetic only. Fix re-review pending.

Owner identity increment: 30f3890888eb79d5f78b72bca5ebe7a935e4a7e9; private middleware sets study_owner_id from configured verified owner identity (local-owner for local-only deployment), excludes every public quiz path. Owner/access/public-route tests: 46 passed.

Remaining O1: GPT lifecycle, queue/source manifest, shared application singleton and transcript/outline wiring, bank review bridge, chat/progress tables/routes. No provider/Windows/deployment evidence.
