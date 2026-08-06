# Quiz Builder operations

## Install or update

Use Python 3.12. Create an isolated environment and install the runtime,
development, document-processing, and PDF-inspection dependencies together:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,document-processing,pdf-inspection]"
.\.venv\Scripts\python.exe -c "import anydoc, pdf_inspector; print('document processors ready')"
```

Copy `.env.example` to an untracked `.env`, set local paths and the loopback
dashboard configuration, then start the Hub. Do not put provider keys, cookies,
or NotebookLM browser state in documentation, release archives, or source
control.

```powershell
Copy-Item .env.example .env
.\.venv\Scripts\oms-hub.exe serve
```

Use an explicit production configuration such as
`OMS_HUB_DASHBOARD_PORT=8765` for `http://127.0.0.1:8765`. Use a separate test
configuration such as `OMS_HUB_DASHBOARD_PORT=8787` for
`http://127.0.0.1:8787` when exercising copied test data. Never test an import
workflow against the live Hub before the test-Hub acceptance pass is complete.

## Configure models

Open **Settings → AI providers**, save provider credentials through the local
credential flow, test each connection, and choose the model assignment for all
three tasks:

- **Quiz question extraction** turns canonical imported documents into cited
  practice-question drafts.
- **Missing-answer generation** is used only after NotebookLM explicitly says
  that the selected supporting sources have no answer support.
- **Accuracy review** controls the medical-accuracy review gate for publication
  workflows where it is enabled.

Do not use an untested provider/model combination for a release. Record the
tested provider and model versions with the release evidence, not their
credentials.

## Parser modes and rollback

`OMS_HUB_DOCUMENT_PARSER_MODE` controls lecture-document evaluation:

- `shadow` is the default. It compares Anydoc with the legacy PPTX baseline but
  never blocks the preserved-PPTX/PDF filing workflow.
- `anydoc` uses enriched Anydoc semantics only when the shadow comparison has
  no promotion blockers; otherwise it returns the legacy result and records a
  degraded fallback.
- `legacy` disables candidate parsing and uses only the existing baseline.

To roll back parser activation, set the local `.env` value and restart the
service:

```powershell
OMS_HUB_DOCUMENT_PARSER_MODE=legacy
```

No source data needs to be deleted or recopied for this rollback. Preserve the
existing comparison reports for diagnosis; they contain metrics and stable
fingerprints rather than source text or credentials.

## Corpus promotion gate

Before changing any production host from `shadow` to `anydoc`, evaluate a
representative, read-only PPTX corpus:

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_anydoc_corpus.py `
  --root "$env:USERPROFILE\Documents\OMS II" `
  --output "C:\ProgramData\OMSStudyHub\document-processing\corpus-report.json"
```

The command writes one atomic aggregate report and exits nonzero for promotion
blockers. Review every blocker, including parse failure, empty candidate output,
warning fingerprints, normalized-text mismatch, lost page/slide coverage,
lower note/table/image counts, ambiguous asset provenance, and `no comparable
PPTX files`. A clean exit is necessary but not sufficient: manually inspect the
corpus report and acceptance evidence before activation.

## Import and answer recovery

Import Practice Questions snapshots local question files, answer keys, or
supporting references before processing. A supplied, unambiguous answer is
stored as `provided_by_source`; it does not call NotebookLM or the AI fallback.

For a missing answer, the worker asks NotebookLM only when an attached
supporting reference is available. A NotebookLM authentication, network,
quota, or service failure records the run failure/retry state and **does not**
silently generate an answer. The AI fallback is allowed only after NotebookLM
returns explicit no-support. Retry the failed source/run after repairing the
connection; do not substitute a generated answer for a NotebookLM failure.

Answers marked `generated_by_ai` require question-level human verification in
the review page. Publication returns a conflict until every required generated
answer is verified. Review the cited source, correct the question if needed,
use **Verify answer**, then retry publication. A manual edit keeps its own
provenance and must still satisfy all review blockers.

If a run is interrupted, restart the Hub and use the existing Studio run. The
durable worker reuses valid parse/extraction artifacts and retries only the
failed stage when its immutable input signature still matches. Do not delete the
Studio database or source snapshots as a recovery shortcut.

## Release evidence

On the test Hub at port 8787, manually verify a supplied-answer import, a
NotebookLM-answer import, and an explicit-no-support AI-fallback import. Confirm
that the fallback quiz cannot publish before per-question verification, then
exercise both Quizzes and Practice Questions libraries, player navigation,
reset, flags, summaries, and media. Record the tested Anydoc, PDF-Inspector,
Python, provider, and model versions in the release notes.

Windows CI installs and imports the document processors with Python 3.12 but
does not validate desktop Office automation. Those tests remain marked
`windows_office`; run them only on a controlled Windows machine with Microsoft
Office installed and record that native evidence separately.
