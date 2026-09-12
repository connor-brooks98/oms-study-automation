# GPT candidate: configuration and study workflow

**Local candidate, not an activated or deployed replacement.** The pinned target
runtime must prove tool prevention, restricted Windows execution/session storage,
and actual subscription model/image/schema behavior before generation can run.
The client currently returns `capability_unverified` before dispatch. No model
prompt or configuration switch bypasses that gate. See the exact
[release proposal](../implementation/gpt-platform-release-proposal.md).

After those gates and the corresponding operation are approved, the operator
configures `OMS_HUB_STUDY_BACKEND=codex_subscription`, a verified absolute
`OMS_HUB_CODEX_EXECUTABLE`, and its `OMS_HUB_CODEX_BINARY_SHA256`. Select the model
through Settings, or supply the exact advertised `OMS_HUB_CODEX_MODEL` as the
initial value. The saved Settings model takes precedence; jobs freeze the selected
model. Never copy `.env.example` over an existing deployment configuration or
change the live Hub's port 8765 as part of this setup.

In **Settings → Study generation**, **Connect ChatGPT** opens the actual managed
login link/code. It never asks for a password. **Check connection** distinguishes
account connection, advertised image inputs, and generation readiness. A login
challenge is not a successful connection. The kickoff's one challenge expired;
its code must not be reused or automatically retried. The executable pin and
protocol are documented in [the session contract](codex-session-contract.md).

Once activated, the owner workflow is:

1. Upload the lecture slides and transcript and resolve any source-review issues.
   Cleaning and quiz generation use the approved current sources. An outline is
   optional; the GPT path does not require a Google notebook.
2. On the lecture page, enter one learning objective per line, choose whether
   original lecture images are required, and create the quiz for review.
3. Review every generated answer and required image. Publication and downloads
   reject incomplete review, missing images, changed sources and lost objective
   coverage. Cancellation/usage limits retain work; resume is explicit and an
   ambiguous dispatched turn cannot be silently repeated.
4. Open the private preview, download JSON/ZIP/PDF, or publish the native quiz.
   ZIP carries the original sanitized PNGs and source/objective manifest. PDF
   separates questions from answers and embeds medical-capable fonts; unsupported
   text fails export instead of silently changing it. Post-publication downloads
   remain on the private lecture page and recheck current evidence.
5. Use **Study chat** for general or selected-lecture questions. AMBOSS reference
   mode explicitly reports unavailable until documented entitlement exists.
   **Study with progress** in the owner quiz library starts a server-recorded
   session. Anonymous public grading does not write personal performance.

[Question-bank imports](../question-bank-import-format.md) accept the documented
normalized format, with separate results-only and authorized-content handling.
Actual UWorld/TrueLearn adapters require the requested supported samples. Manual
Anki candidates read only an already configured local index; full curation,
scheduling changes and live Anki writes are outside this candidate.

Fresh GPT outlines can be filed with current-source checks. Replacing any retained
imported NotebookLM outline through GPT is explicitly unavailable; its existing
reviewed legacy replacement and historical proof graph remain intact.
