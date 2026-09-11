# Question bank import format

Study Hub normalized JSON v1 is the only implemented parser. Q1 provides a local,
read-only preview. Persistence, import UI, Anki matching and publication are later
tasks; a successful preview does not mean anything has been imported or published.

## Support and provenance

| Input | Current support |
| --- | --- |
| Study Hub normalized v1 JSON | Validated with synthetic fixtures |
| UWorld export | Adapter unavailable; actual supported sample needed |
| TrueLearn export | Adapter unavailable; actual supported sample needed |
| Manual AMBOSS NID/query | Separate Q4 task; not accepted by this parser |
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
multiplicity do. The future commit boundary must revalidate and recompute the
digest rather than trusting a caller-created preview. Models prevent field
reassignment; raw nested question dictionaries remain ordinary JSON containers.

Within one envelope, duplicate row identity is `(question_id, attempt_id)`.
Complete canonical equality produces `duplicate_row`; changed content produces
`duplicate_conflict`, which must block the whole future commit. Repeated identical
rows are excluded from ready-row enumeration. The envelope retains all submitted
rows for provenance, and issues always use their original one-based row numbers.
Distinct attempt IDs for the same QID remain separate. Cross-export/learner replay
and conflicting question versions are Q3 transaction checks, not preview writes.

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
