# Anki Lecture Selector and Source Binding Design

## Goal

Repair the Anki curation start page so every lecture exposes its actual current
slide and transcript revisions, and replace the flat lecture select with a
Course → Exam → lecture accordion. Improve the form alignment and populate the
canonical lecture tag as editable input text when a lecture is selected.

The change is limited to the Anki start page. It does not alter ingestion
records, Anki indexes, curation contracts, acceptance-copy data, or the
review/apply workflow.

## Root Cause

The current template serializes each lecture's revision list with `tojson`
inside a double-quoted HTML data attribute. JSON contains double quotes, so the
browser truncates the attribute. The client then catches the resulting JSON
parse failure and treats every selected lecture as having zero current
revisions.

Revision data will instead be serialized once in an
`application/json` script element. Lecture controls will carry only the numeric
lecture ID, and the client will resolve the selected lecture from the parsed
payload. This avoids quote-sensitive JSON attributes.

## Selector and Data Flow

The server will return lectures grouped in the established catalog order:

1. Course
2. Exam
3. Lecture number and topic

The page will render nested native `details` elements for courses and exams.
Each lecture will be a button labeled `Lecture <number> — <topic>`. Only one
lecture may be selected. The selected button will expose an accessible selected
state, populate a hidden `lecture_id` form field, and render the current source
revision checkboxes below the accordion.

Slides and transcript revisions remain independently selectable and default to
checked. A lecture with genuinely no current revisions will retain the existing
empty-state message. Job creation continues to submit the same
`CreateCurationJobRequest`; no API or database migration is needed.

## Editable Lecture Tag

Each lecture payload will include the established canonical default produced by
`oms_hub.anki.paths.target_tag`:

```text
AnkiHub_Optional::LMU_OMS_II::<CourseWithoutSpaces>::Block<Exam>::Lec<Lecture>_<Topic>
```

Selecting a lecture will assign this value to the `target_tag` input's actual
value. It will not be a placeholder. The user may edit it before starting
curation. Selecting another lecture will replace the field with the newly
selected lecture's canonical default.

## Form Alignment

The form will keep a responsive two-column layout while aligning paired labels,
controls, and help text at consistent row starts. The lecture selector, source
selector, lecture tag, and focus text remain full-width. Related deck scope and
destination fields will use equal-width columns. Inputs, selects, and textareas
will have consistent widths and label spacing, and the mobile breakpoint will
collapse the layout to one column without offsets.

## Error Handling

- Invalid embedded lecture JSON disables lecture selection and shows a concise
  setup error instead of claiming the lectures have no sources.
- A selected lecture ID absent from the payload is rejected client-side.
- A lecture with no current source revisions shows the truthful existing
  empty-state message.
- Job submission remains blocked until a lecture and at least one source
  revision are selected.

## Verification

Regression coverage will prove:

- bootstrap/template output includes current revision IDs and kinds;
- the lecture payload is valid when topics and filenames contain quotes or
  apostrophes;
- lectures are grouped Course → Exam → lecture in deterministic order;
- selecting a lecture reveals its slides/transcript revisions;
- selecting a lecture fills the canonical tag as editable text;
- selecting a different lecture replaces the default tag;
- the hidden lecture ID and selected source revision IDs preserve the existing
  job request contract;
- the responsive alignment classes render without changing review/apply pages.

Focused Python and JavaScript tests will run before the full repository test,
Ruff, mypy, and diff checks.
