# NotebookLM Studio rollout

This rollout applies branch `codex/notebooklm-studio-main-hardening`. It adds a private course/exam NotebookLM chat workspace while preserving the existing semi-public quiz URLs and grading contract.

## Safety boundary

- Studio source attachment uses NotebookLM `add_file`, `add_text`, and `add_url`.
- Quiz generation uses only `chat.ask(..., source_ids=...)`, including an explicit empty list for prompt-only runs.
- Study Hub never calls a NotebookLM Studio quiz or artifact-generation API.
- The local publisher only validates the chat response with `parse_native_quiz`, replaces its title with the approved label, and stores it in the existing token-based quiz system.

## Upgrade

1. Stop the Study Hub scheduled task.
2. Make timestamped copies of the database, data directory, and encrypted NotebookLM storage.
3. Switch to the branch and install the project into the existing virtual environment.
4. Start Study Hub and wait for `/health` to return `status: ok`.
5. Confirm the schema version is `10`.

Migration 10 rebuilds `published_quizzes` to support either a lecture/job origin or a Studio-run origin, backfills existing lecture quizzes with their course/exam destination, and preserves their tokens, payloads, and versions. It also adds Studio label and publication fields.

## Acceptance on the Windows NUC

1. Open **NotebookLM Studio** and attach one PDF, one PPTX, pasted text, and one website/YouTube URL.
2. Confirm PPTX becomes a validated PDF and that a deliberately hung conversion times out without leaving a partial output; a later conversion must still work.
3. Confirm NotebookLM login storage remains encrypted at rest and no plaintext storage file remains after login or source/run operations.
4. Run the professor-URL case with one selected URL source and publish it to a different course/exam.
5. Run a prompt-only case with no selected sources.
6. Confirm both quizzes appear in the selected public course/exam, reveal no answers before POST submission, and grade normally after submission.
7. Re-run one quiz and confirm its token remains stable while its version increments.
8. Delete a source and confirm it is removed from NotebookLM/future pickers without changing a completed quiz.
9. Unpublish a Studio quiz and confirm its token returns 404 while private run/response history remains visible.
10. Expire the NotebookLM session and confirm only the affected source/run fails and Settings requests reconnection.

## Rollback

Stop Study Hub and restore the timestamped database and data-directory backup before reinstalling the previous commit. Do not run the previous build against a database already migrated to version 10; its published-quiz model does not understand Studio origins.
