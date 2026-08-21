# Study Hub frozen repository map v1

Gate 0 was captured from `749d729010aa75bf160f996d39e11edccb883a58`
(`c20af8e2df4991852014ec9f1f66462e5363d71d`). The machine-readable,
schema-constrained counterpart is
`artifacts/implementation/repo-map-v1.json`.

## Observed composition

`src/oms_hub/app.py:create_app` creates `Database`, calls `database.migrate()`,
then mounts `/static` from `src/oms_hub/web/static` and registers the central,
Anki, artifact, settings, upload, quarantine, generation, lecture, Studio,
published-quiz, and public-quiz routers. `Database.migrate()` delegates to
`migrations.migrate_database`; models are rooted in `src/oms_hub/models.py` and
`src/oms_hub/anki/models.py`. The observed frontend root is `src/oms_hub/web/`,
with `templates/` and `static/` below it.

## Frozen paths

| Area | Repository-relative paths |
| --- | --- |
| Database/bootstrap | `src/oms_hub/app.py`, `src/oms_hub/db.py`, `src/oms_hub/migrations.py`, `src/oms_hub/models.py`, `src/oms_hub/anki/models.py` |
| Configuration/secrets | `src/oms_hub/config.py`, `src/oms_hub/security/secret_store.py`, `src/oms_hub/web/settings_routes.py`, `.env.example` |
| Central routes | `src/oms_hub/app.py` |
| Navigation/shell | `src/oms_hub/web/templates/base.html`, `src/oms_hub/web/static/study_hub_shell.js`, `src/oms_hub/web/static/study-hub.css`, `src/oms_hub/web/static/tokens.css`, `src/oms_hub/web/static/reset.css`, `src/oms_hub/web/static/app.css` |
| Main Hub lecture | `src/oms_hub/web/routes.py`, `src/oms_hub/web/templates/lecture.html`, `src/oms_hub/web/static/lecture.js` |
| Public quiz player | `src/oms_hub/web/public_quiz_routes.py`, `src/oms_hub/web/templates/public_quiz.html`, `src/oms_hub/web/static/public_quiz.js`, `src/oms_hub/web/static/public_quiz.css` |
| Dashboard | `src/oms_hub/web/routes.py`, `src/oms_hub/web/templates/dashboard.html`, `src/oms_hub/web/static/dashboard.js` |
| Artifact/private preview | `src/oms_hub/web/artifact_routes.py`, `src/oms_hub/web/templates/artifact_text.html` |
| Outline generation | `src/oms_hub/web/generation_routes.py`, `src/oms_hub/study_generation/service.py`, `src/oms_hub/study_generation/worker.py`, `src/oms_hub/study_generation/outline.py`, `src/oms_hub/study_generation/repository.py` |
| Lecture quiz generation | `src/oms_hub/web/generation_routes.py`, `src/oms_hub/study_generation/service.py`, `src/oms_hub/study_generation/worker.py`, `src/oms_hub/study_generation/native_quiz.py`, `src/oms_hub/study_generation/repository.py` |
| Custom quiz/Studio | `src/oms_hub/web/studio_routes.py`, `src/oms_hub/web/templates/notebook_studio.html`, `src/oms_hub/study_generation/studio_service.py`, `src/oms_hub/study_generation/studio_worker.py`, `src/oms_hub/study_generation/studio_repository.py`, `src/oms_hub/study_generation/quiz_import_worker.py`, `src/oms_hub/study_generation/practice_review.py`, `src/oms_hub/study_generation/quiz_images.py`, `src/oms_hub/models.py` |
| Learner quiz attempt boundary | `src/oms_hub/web/public_quiz_routes.py` |
| Anki v2/local and agent boundaries | `src/oms_hub/anki/ankiconnect.py`, `src/oms_hub/anki/runtime.py`, `src/oms_hub/anki/apply.py`, `src/oms_hub/web/anki_routes.py`, `src/oms_hub/web/anki_agent_routes.py`, `src/oms_anki_agent`, `scripts/macos/com.omsstudy.anki-agent.plist`, `src/oms_hub/app.py` |
| CI | `.github/workflows/ci.yml` |

## Commands copied from CI

```text
Install: python -m pip install -e ".[dev,document-processing,pdf-inspection]"
Python lint: ruff check src tests scripts
Python types: mypy src
Python tests: PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring pytest -q -m "not windows_office"
JavaScript tests: node --test "tests/js/*.test.js"
Windows document tests: $env:PYTEST_ADDOPTS='-m "not windows_office"'; .\.venv\Scripts\python.exe -m pytest tests\test_office_converter.py tests\document_processing tests\study_generation tests\v2 -q
Deployment interface: .\scripts\install-windows.ps1
```

The deployment interface is documented only and was not run. There is no
`package.json`, Makefile, justfile, tox, or nox configuration at Gate 0.

The hosted Windows baseline is separately preserved in
`artifacts/acceptance/grounded-learning/baseline/windows-ci-32254255685.json`.
It is a narrow redacted capture of the exact CI run/job/failure; it is not a
local Windows pass.

## Boundary note: learner attempts

`POST /quizzes/{token}/answer` in `public_quiz_routes.py` invokes
`grade_answer` and immediately returns correctness, the correct choice ID, and
rationale. It does not write a learner attempt record. Existing
`generation_attempts` and `studio_run_attempts` are generation/run retry
records, not learner quiz-attempt persistence. Grounded-learning work must
treat learner-attempt persistence as absent at this baseline.
