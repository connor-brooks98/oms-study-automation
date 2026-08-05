# Transcript Cleaning Prompt Setting — Design

Date: 2026-08-04
Status: Approved
Branch: `codex/anki-v4-unified-providers-and-review-fixes`

## Goal

Allow the transcript-cleaning prompt file to be selected, saved, and tested from
Settings beside the existing lecture outline and quiz prompt controls. The saved
path takes effect when Study Hub is restarted after the branch is installed.

## User experience

The existing Notebook prompts section becomes a general prompt-files section with
three matching cards:

1. Transcript cleaning prompt
2. Lecture outline prompt
3. Lecture quiz prompt

The transcript card uses the same Select Path, Save Path, Test file, inline status,
and CSRF-protected request behavior as the existing cards. Testing validates the
file without returning prompt contents to the browser.

## Persistence and startup behavior

Add `transcript` to the persisted prompt-kind domain and store its path in the
existing `study_prompt_settings` table. No schema change is needed because the
table already stores prompt kinds as strings.

At application startup, resolve the transcript prompt path in this order:

1. Saved `study_prompt_settings` transcript path.
2. `OMS_HUB_TRANSCRIPT_PROMPT_PATH` as a backward-compatible fallback.
3. Unconfigured when neither value exists.

The configured approval hash remains sourced from
`OMS_HUB_TRANSCRIPT_PROMPT_SHA256`; this change does not weaken the existing prompt
approval requirement. Changing the path therefore requires the prompt at the new
location to match the approved hash before transcript processing can proceed.

## Validation

The transcript Test file action uses the transcript prompt loader's current rules:

- path is configured and readable;
- file is nonempty UTF-8;
- file size is at most 64 KiB;
- the response includes state and SHA-256 but never prompt contents.

Outline and quiz testing continues through `PromptFileService` unchanged.

## Compatibility and error handling

- Existing installations using only the environment variable continue to work.
- Saving an invalid or blank path returns the same validation-style response used
  by the existing prompt cards.
- A missing or invalid transcript file is reported inline on Settings and does not
  expose local file contents.
- The saved path is applied after the Hub restarts; no running worker is hot-swapped.

## Testing

- Route test: save and test a transcript prompt without returning its contents.
- Settings-page test: all three prompt cards render in the intended order.
- Startup test: the database path overrides the environment path.
- Compatibility test: the environment path remains the fallback when no database
  path is saved.
- Existing Python, JavaScript, Ruff, and mypy suites remain green.

## Out of scope

- Editing prompt contents in the browser.
- Changing or approving the transcript prompt SHA-256 from Settings.
- Hot-reloading the prompt in an already running transcript worker.
