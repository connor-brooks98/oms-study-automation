# O: task provider routing — 2026-09-15

User clarification: GPT powers the main lecture features; transcript cleaning and
imported practice-question processing must honor their separate provider/model choices.

Changed application wiring so **new transcript jobs** use the existing Transcript
cleaning task assignment. Previously `study_backend=codex_subscription` overrode
that assignment for every new transcript upload. Main lecture generation remains
on its GPT backend. Previously queued transcript jobs retain their recorded backend;
no migration or job rewriting is performed. Missing credentials fail through the
selected provider's existing diagnostic, without switching providers.

Imported question extraction already uses the Quiz question extraction assignment;
no production change was needed. A real-app regression confirms selected Gemini
transcript and Anthropic import model IDs reach only their inert test adapters while
the main lecture backend remains Codex. Existing subscription transcript acceptance
explicitly exercises retained subscription jobs rather than relying on the old default.

**Separate limitation:** missing-answer generation for imported questions remains
disabled by the earlier NotebookLM upload-only change. Its visible task assignment
does not currently trigger generation. Restoring that capability requires an explicit
answer-generation action or equivalent authorization boundary so older blocked imports
do not silently begin provider requests. The resolver and import worker are unchanged
in this patch; supplied answers and manual review retain existing behavior.

Validation: focused ingestion, reservation, retained GPT lecture acceptance, import
extraction/answer, and provider settings/UI suites passed (one existing opt-in skip).
LLM service/repository suites passed; focused Ruff and whitespace checks passed.
All provider adapters used by the routing regression are inert fixtures. No provider
request, credentials read, purchase, fallback activation, process restart or deployment
was performed.
