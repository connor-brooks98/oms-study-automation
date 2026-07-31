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
3. Switch to the branch and reinstall the project dependencies into the existing virtual environment. This upgrade adds Pillow for safe image decoding and sanitization, so an editable-code-only update is not sufficient.
4. Start Study Hub and wait for `/health` to return `status: ok`.
5. Confirm the schema version is `11` after the first restart.

Migration 10 rebuilds `published_quizzes` to support either a lecture/job origin or a Studio-run origin, backfills existing lecture quizzes with their course/exam destination, and preserves their tokens, payloads, and versions. Migration 11 adds private Studio drafts, grouped image requirements, per-question overrides, and immutable published-media bindings. It does not change existing quiz tokens or content.

## Acceptance on the Windows NUC

1. Open **NotebookLM Studio** and attach one PDF, one PPTX, pasted text, and one website/YouTube URL.
2. Confirm PPTX becomes a validated PDF and that a deliberately hung conversion times out without leaving a partial output; a later conversion must still work.
3. Confirm NotebookLM login storage remains encrypted at rest and no plaintext storage file remains after login or source/run operations.
4. Run one image-free professor-URL quiz. Confirm it publishes automatically and appears in the selected public course/exam.
5. Run one quiz whose NotebookLM chat response identifies an image used by multiple questions. Confirm the run shows **Images needed** and **Add images** and does not replace the currently published quiz.
6. Upload one still PNG, JPEG, or WebP image for the shared image key. Confirm every question using that key resolves to the same upload.
7. Turn on a per-question text-only override and then reverse it. Confirm the question first stops requiring the image and then requires the shared image again.
8. Confirm **Preview quiz** appears only after every image requirement is resolved. Open it, inspect every question and image, and use **Publish quiz** to make the reviewed version public.
9. Open the public quiz from the persistent **Quiz Library** top-navigation link. Confirm the uploaded image is displayed above each applicable question, opens full-size, and the quiz reveals no answers before POST submission.
10. Run a prompt-only case with no selected sources and confirm it still publishes automatically.
11. Re-run one quiz and confirm its token remains stable while its version increments only after publication.
12. Delete a source and confirm it is removed from NotebookLM/future pickers without changing a completed quiz.
13. Unpublish a Studio quiz and confirm its token and media return 404 while private run/response history remains visible.
14. Expire the NotebookLM session and confirm only the affected source/run fails and Settings requests reconnection.

## Rollback

Stop Study Hub and restore the timestamped database and data-directory backup before reinstalling the previous commit. Do not run the previous build against a database already migrated to version 11; it does not understand the Studio image-review tables or draft state.
