# Question bank import format

Study Hub normalized JSON v1 supports private preview, confirmed import, stored
receipts, exact configured Anki tag matching, and staging authorized question bodies
into the existing quiz review. Importing results never publishes a quiz. Evidence
below uses synthetic inputs and temporary databases; actual vendor exports remain
unvalidated.

## Support and provenance

| Input | Current support |
| --- | --- |
| Study Hub normalized v1 JSON | Preview, persistence, replay, rollback and reviewed publication validated with synthetic fixtures |
| UWorld export | Adapter unavailable; actual supported sample needed |
| TrueLearn export | Adapter unavailable; actual supported sample needed |
| Manual AMBOSS NID/query | Separate owner-only candidate form; bounded NID parsing and supplied snapshot preview |
| Previously inspected GitHub question dataset | Held; not imported or treated as vendor QIDs |

`source` is one of `uworld`, `truelearn`, `amboss`, `study_hub`, `user`. It labels
the supplied identity, not verified vendor provenance. `product` identifies the
specific bank/product. Identity is the exact `(source, product, question_id)` string
tuple: preserve case and leading zeros. `00123` and `123` are distinct.

The [synthetic fixture](../tests/question_bank/fixtures/normalized-v1.json) is a
complete example. It contains a result and user note, not a full vendor question.

```json
{
  "schema_version": 1,
  "source": "user",
  "product": "personal-study",
  "export_id": "notes-2026-09-11",
  "provenance": {"kind": "user_notes", "description": "My own notes"},
  "rows": [{"question_id": "00123", "user_note": "Review this topic"}]
}
```

## Fields and limits

The top-level fields shown above are required; `rows` contains 1–10,000 objects.
Unknown fields are rejected in the envelope, provenance, rows and topic labels.
The byte limit is 10 MiB, including whitespace. Invalid JSON, numeric IDs, invalid
types, NaN/Infinity and overflowed nonfinite numbers are rejected with `ValueError`.
JSON strings remain data: no expression evaluation, path access or URL fetching.

| Row field | Meaning/default |
| --- | --- |
| `question_id` | Required exact string, 1–200 characters |
| `attempt_id` | Exact string, 1–200 characters, or `null` (default) |
| `result` | `correct`, `incorrect`, `omitted`, or `unknown` (default) |
| `occurred_at` | Timezone-aware datetime string, or `null` (default) |
| `elapsed_ms` | Nonnegative integer, or `null` (default); booleans are invalid |
| `user_note` | String of 0–20,000 characters, default empty |
| `tags` | Array of original labels, each 1–300 characters, default empty |
| `topics` | Array of topic objects below, default empty |
| `question` | One native quiz question object, or `null` (default) |

`product` is 1–100 characters; `export_id` is 1–200. Identity fields cannot be blank
or have surrounding whitespace. They are never trimmed or case-normalized.
`attempt_id: null` requires `result: unknown`, `occurred_at: null`, and
`elapsed_ms: null`. A QID alone cannot invent an attempt or a correctness result.
Timestamp strings must begin with an explicit `YYYY-MM-DD` date and a date/time
separator (`T`, `t`, or space), followed by a valid time and timezone. Numeric
strings such as `1789140000` or `20260911` are rejected rather than guessed as epochs.
An explicit attempt may still have unknown outcome or missing time. Omitted and
unknown outcomes remain distinct from incorrect answers.

Topic objects require `axis` (`system`, `discipline`, `topic`, `exam`, `course`,
`lecture`, `objective`) and `label` (1–300 characters). Optional fields are
`canonical_id` (exact nonblank string of 1–200 characters or null), `method`
(`unmapped`, `exact`, `user`, `model`; default `unmapped`), `confidence` (finite
number 0–1 or null), and `review_state` (`pending`, `accepted`, `rejected`; default
`pending`). Supplied review metadata is preserved; parsing it does not authenticate
a reviewer or authorize overwriting a previously accepted mapping.

## Full question readiness

`provenance` requires a string `description` and a `kind` of `user_results`,
`user_notes`, or `authorized_question_export`. A non-null question requires
`authorized_question_export`; otherwise the whole input is rejected. The caller
must establish the actual right to import that content; the supplied label alone
is not proof of permission.

The body is checked with the existing `parse_native_quiz` contract by wrapping it
in a one-question quiz. Answer indices, choice uniqueness, rationales and image
references use that existing contract. A malformed body, missing answer or
`correct_index: -1` produces an `invalid_question` issue and remains held. Its
metadata/result and original body remain available for review. Image-dependent
questions receive `missing_image` even when the image reference parses: normalized
v1 does not transport assets or retrieve remote files. The existing media upload
and reviewed publication paths must establish readiness later.

`ready_question_rows` means structurally valid, image-free bodies at preview time.
It does not establish source accuracy, medical review, successful persistence or
permission to publish. External QIDs stay separate from temporary native `q1` IDs.

## Preview digest and duplicates

`parse_import(raw: bytes)` returns the validated envelope. `preview_import(raw)`
returns that envelope, its SHA-256 digest, issues and ready row numbers. Neither
function writes files/databases, contacts providers/Anki, or publishes a quiz.

The digest hashes UTF-8 `json.dumps(envelope.model_dump(mode='json'), sort_keys=True,
separators=(',', ':'), allow_nan=False)` with Python's default ASCII escaping.
Whitespace and object-key order do not change it. Array order and original row
multiplicity do. The commit boundary revalidates and recomputes the digest rather than trusting a
caller-created preview. The 10 MiB cap applies to uploaded bytes; internal ASCII
escaping can expand valid Unicode documents past that size without rejecting their
confirmation or receipt reload. Models prevent field
reassignment; raw nested question dictionaries remain ordinary JSON containers.

Within one envelope, duplicate row identity is `(question_id, attempt_id)`.
Complete canonical equality produces `duplicate_row`; changed content produces
`duplicate_conflict`, which blocks the whole commit. Repeated identical
rows are excluded from ready-row enumeration. The envelope retains all submitted
rows for provenance, and issues always use their original one-based row numbers.
Distinct attempt IDs for the same QID remain separate. Cross-export replay and conflicting question versions are transaction checks, not
preview writes. Exports and attempts are scoped to the learner. Reconfirmation of
the same export returns its stored receipt without adding rows or attempts. Another
export can retain original duplicate rows without adding the same attempt twice.
A changed row for an existing attempt, changed export identity, or conflicting
non-null question body rolls back the entire new import. Original notes, tags,
result and nullable timestamps remain available through the owner-only receipt.

## Private import and review workflow

Open `/question-bank`, upload normalized JSON, inspect the original rows/issues,
then confirm the same file and preview fingerprint. Preview writes nothing.
Confirmation redirects to `/question-bank/imports/{import_id}`; only that owner
can read its receipt, export matched NIDs, or stage question review. Missing owner
is 401, missing/invalid CSRF is 403, another owner's receipt is 404, conflicting
content/fingerprint is 409, invalid input is 422, and oversized uploads are 413.

Receipts retain every original row, including duplicates and held bodies. Counts
refer to different things: saved records, newly created attempts, and question-body
readiness. A held body can coexist with a successfully saved result or note.
Duplicate counts are reported separately and do not mean original rows were lost.
Question identities with no body may receive their first body later; a non-null
body hash cannot be overwritten by another import, even if the old body is held.

Select at most 500 original rows to enter the existing `/studio/runs/{id}/review`.
Over 500 reviewable rows require explicit smaller batches. Results-only records,
exact duplicate rows and unsupported body structures are excluded. Missing answers,
rationales and image assets stay blocked in review. Every imported question needs
source-answer verification; image-dependent questions also need the existing media
upload. Only existing reviewed publication makes a public quiz. Import and staging
do not enqueue generation or publish content. Native q1..q500 identifiers retain
an exact link to the original source/product/QID and import row.

Imported topic claims are pending proposals, including claimed accepted mappings.
Authorized topic review uses the existing content hash and audit record; original
row metadata is retained. `list_ready_questions` returns only bodies available
through this learner's own body-ready imports, filtered by accepted canonical
mappings. It is a structural-content lookup, not proof of reviewed publication.

## Read-only Anki candidates

Exact bank matching needs an explicitly supplied local index and reviewed rules
mapping each source/product to a complete tag prefix. Tags must equal prefix plus
the original QID; substring matches, guessed topics and cross-source prefix reuse
are rejected. Every candidate exposes all snapshot card IDs, pending review, and
unverified freshness. `/question-bank/imports/{import_id}/anki-export` downloads
only generated numeric NID searches; no matches returns 409, never an empty search.

The separate `/question-bank/anki-candidates` GET/POST form accepts manually pasted
queries such as `nid:123,456 OR nid:789`. It accepts only positive signed-64-bit
note IDs, comma lists and case-insensitive OR, up to 64 KiB and 2,000 total IDs
before deduplication. Other search fields/operators and internal newlines are
rejected with 422 before index lookup. IDs are deduplicated in first-seen order.
Missing notes remain explicit; existing notes show normalized text and every card.
An absent index/snapshot returns 503. A snapshot change during preview returns 422.

The form requires the owner and existing CSRF checks. Its provenance is
`user_pasted_amboss_candidates`, displayed as unverified; it establishes neither
AMBOSS entitlement nor a verified question-to-note association. The router's default
configuration has no Anki index. These paths never refresh a snapshot, call Anki RPC,
change notes/tags/schedules, access credentials, or contact AMBOSS. O owns the chat
entry link and app wiring, passing only an already configured
`app.state.anki_companion_index`. Manual NIDs work independently of tag rules.
Exact QID `tag_rules` remains empty until real prefixes are observed and reviewed;
matching is fixture-tested, with actual source-to-tag mapping pending. Full
curation is separate work.

## Acceptance evidence and consumer boundary

The synthetic reconciliation in
`tests/question_bank/test_native_review.py::test_normalized_acceptance_reconciles_records_replay_and_publication`
submits eight rows: eight retained original records, zero blocked record writes,
one duplicate reported separately, seven question identities, and four attempts
(correct, incorrect, omitted, unknown). The body breakdown is one ready, two held,
four metadata-only, and one duplicate. Replay leaves four attempts. A second source
using the same `00123` adds one distinct attempt. A later conflicting two-row export
rolls back both rows and leaves five attempts. Staging includes three bodies, keeps
the unknown answer and missing image blocked, and creates no published quiz.

The adjacent reviewed-publication test attaches a synthetic image and verifies
answers through existing services, then checks the existing public player/content/
image routes. Repository tests cover concurrent replay and injected-write rollback;
route tests cover private access, CSRF, Unicode wire-size expansion and candidates.

`AttemptFact` supplies `id`, `learner_id`, exact `key`, `attempt_id`, `result`, nullable
`occurred_at`/`elapsed_ms`, `imported_at`, `import_id`, and effective `topics`.
`iter_attempts(learner_id=..., after_id=0, limit=500)` uses stable ascending keyset
pagination. An unknown result remains unknown; imported time never substitutes for
attempt time. Analytics must exclude unknown and omitted outcomes from correctness
denominators. Q6 verifies preserved facts; actual consumer-denominator acceptance
awaits O's cross-suite run after the separately owned C3 analytics is accepted.

## Samples needed for vendor adapters

For each vendor independently, supply an actual supported export or a user-made
results/QID/notes file, with sensitive account details removed. Include the product,
export menu/steps, original file type, timezone, and how stable question and attempt
or block identifiers are obtained. Keep representative headers/field names and
examples of repeated attempts, unknown/omitted results, notes and tags when present.
Identify whether it contains full questions or only results/QIDs/notes and establish
authorized provenance for any question bodies/assets. An aggregate-only report
cannot establish individual attempts. Without stable attempt keys, QID/note-only
import remains possible. No credentials or authenticated scraping are needed.

No vendor adapter or claim of real UWorld/TrueLearn export acceptance is made until
that sample is inspected and its exact mapping is covered by a regression fixture.
