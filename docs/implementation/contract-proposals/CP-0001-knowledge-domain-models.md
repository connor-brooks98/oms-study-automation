# CP-0001: Knowledge domain models and `knowledge-v2.json`

Status: proposed and unapplied. This document is not approval or activation.

## Current contract

`schemas/knowledge-v1.json` is the tag-frozen v1 snapshot. It currently covers
the provider wire contracts `RetrievalScope`, `EvidenceRef`,
`RetrievalRequest`, `RetrievalResult`, and `ProviderHealth`, together with
`AuthorityClass` and `TruthMode`. The exporter and this snapshot are immutable
on the Task 1.2 branch.

## Proposed additive v2 contents

Generate a new `knowledge-v2.json` from the frozen slotted dataclasses in
`oms_hub.knowledge.models` plus the existing provider contracts:

- `SourceRevisionState`: `staged`, `normalizing`, `ready`, `stale`, `failed`,
  `retired`.
- `EvidenceLocatorKind`: `page`, `slide`, `speaker_note`,
  `transcript_segment`, `section`, `figure`, `table`, `article_page`.
- `KnowledgeSource`: `source_document_id`, `authority_class`.
- `SourceRevision`: `source_document_id`, canonical `source_revision_id`,
  `file_sha256`, and `state`.
- `EvidenceLocator`: `kind` and `value`.
- `EvidenceUnit`: its stable identity, source revision, authority and course /
  exam / lecture scope, locator, normalized text, optional image asset,
  content hash, source priority, creation timestamp, and optional retirement
  timestamp.

`EvidenceUnit.supports_medical_claims` is a derived Python property and is not
a wire field. `SourceRevision.revision_id` is a read-only Task 1.3 compatibility
property and is also not a wire field. Runtime validation requires `course_id`
for `course_material` evidence; JSON Schema generation should represent the
following exact conditional requirement at the proposed boundary:

```json
{
  "if": {
    "properties": {"authority_class": {"const": "course_material"}},
    "required": ["authority_class"]
  },
  "then": {
    "required": ["course_id"],
    "properties": {"course_id": {"type": "string", "pattern": "\\S"}}
  }
}
```

The proposed `EvidenceUnit` input has exactly these required keys:
`evidence_id`, `source_revision_id`, `authority_class`, `course_id`,
`exam_id`, `lecture_id`, `locator`, `normalized_text`, and `content_sha256`.
`course_id`, `exam_id`, and `lecture_id` remain nullable for non-course
authority classes, while the conditional above requires a non-whitespace
`course_id` string for course material. These keys are omittable/defaulted:
`image_asset_id` defaults to `null`, `source_priority` defaults to `0`,
`created_at` has a UTC-now default, and `retired_at` defaults to `null`.
Ordinary serialization emits all four defaulted/nullable fields, including
`created_at` and `retired_at`.

`created_at` and non-null `retired_at` are UTC ISO-8601 date-times. The v2
schema should use `format: "date-time"` plus
`pattern: "(?:Z|\\+00:00)$"`; runtime construction accepts `Z` and `+00:00`,
requires timezone-aware UTC, and preserves the supplied spelling. The schema
must not expose `supports_medical_claims` or `revision_id` because both are
derived properties.

The exporter change, when authorized, should construct a deterministic
`TypeAdapter` union containing the existing v1 wire models and these additive
domain models, set `$id` to `knowledge-v2.json`, and write only the new file.
It must not rewrite `knowledge-v1.json`.

## Impact

Producers are Task 1.2 model construction, Task 1.3 persistence, and Task 1.4
normalization. Consumers are the source-trust repository/backfill, provider
indexing and citation mapping, Ask retrieval/citation validation, board
question evidence packets, and approved literature registration. No existing
v1 producer or consumer needs to change for this proposal.

## Repository/import invariants

Repository/import activation must resolve every `EvidenceUnit.source_revision_id`
to its `SourceRevision`, then resolve that revision's
`source_document_id` to its `KnowledgeSource`. It must reject an evidence unit
whose `authority_class` differs from the referenced knowledge source's
`authority_class`; it must not silently rewrite either authority. A differing
authority requires a separate logical `source_document_id` and source revision.
The exact proposed activation rejection test is:

```python
from oms_hub.knowledge.ids import sha256_text
from oms_hub.knowledge.models import (
    EvidenceLocator,
    EvidenceLocatorKind,
    EvidenceUnit,
    KnowledgeSource,
    SourceRevision,
    SourceRevisionState,
)
from oms_hub.providers.contracts import AuthorityClass


def test_import_rejects_evidence_authority_mismatch(repository) -> None:
    source = KnowledgeSource("source_1", AuthorityClass.COURSE_MATERIAL)
    revision = SourceRevision("source_1", "sr_1", "a" * 64, SourceRevisionState.READY)
    unit = EvidenceUnit(
        evidence_id="ev_1",
        source_revision_id="sr_1",
        authority_class=AuthorityClass.PUBLISHED_JOURNAL,
        course_id="heme",
        exam_id=None,
        lecture_id=None,
        locator=EvidenceLocator(EvidenceLocatorKind.SECTION, "abstract"),
        normalized_text="text",
        content_sha256=sha256_text("text"),
    )
    repository.create_source(source)
    repository.create_revision(revision)
    with pytest.raises(ValueError, match="authority_class"):
        repository.put_evidence_units("sr_1", (unit,))
```

## Revision-state lifecycle and consumer eligibility

The proposed state transitions are deliberately bounded:

```text
staged      -> normalizing | failed
normalizing -> ready | failed
ready       -> stale | retired
stale       -> retired
failed      -> normalizing | retired
retired     -> (none)
```

Only `ready` may enter new indexing, retrieval, citation, or question-evidence
packets. `staged`, `normalizing`, `failed`, `stale`, and `retired` are excluded
from all new trusted flows. Stale and retired revisions remain immutable and
available for audit/reference preservation; exclusion is not deletion.

The exact proposed lifecycle and consumer tests are:

```python
@pytest.mark.parametrize(
    ("before", "after"),
    [
        (SourceRevisionState.STAGED, SourceRevisionState.NORMALIZING),
        (SourceRevisionState.STAGED, SourceRevisionState.FAILED),
        (SourceRevisionState.NORMALIZING, SourceRevisionState.READY),
        (SourceRevisionState.NORMALIZING, SourceRevisionState.FAILED),
        (SourceRevisionState.READY, SourceRevisionState.STALE),
        (SourceRevisionState.READY, SourceRevisionState.RETIRED),
        (SourceRevisionState.STALE, SourceRevisionState.RETIRED),
        (SourceRevisionState.FAILED, SourceRevisionState.NORMALIZING),
        (SourceRevisionState.FAILED, SourceRevisionState.RETIRED),
    ],
)
def test_source_revision_allows_only_proposed_transitions(before, after) -> None:
    assert can_transition_revision_state(before, after) is True


def test_source_revision_rejects_every_omitted_transition() -> None:
    allowed = {
        SourceRevisionState.STAGED: {
            SourceRevisionState.NORMALIZING,
            SourceRevisionState.FAILED,
        },
        SourceRevisionState.NORMALIZING: {
            SourceRevisionState.READY,
            SourceRevisionState.FAILED,
        },
        SourceRevisionState.READY: {
            SourceRevisionState.STALE,
            SourceRevisionState.RETIRED,
        },
        SourceRevisionState.STALE: {SourceRevisionState.RETIRED},
        SourceRevisionState.FAILED: {
            SourceRevisionState.NORMALIZING,
            SourceRevisionState.RETIRED,
        },
        SourceRevisionState.RETIRED: set(),
    }
    for before in SourceRevisionState:
        for after in SourceRevisionState:
            assert can_transition_revision_state(before, after) is (after in allowed[before])


@pytest.mark.parametrize("state", tuple(SourceRevisionState))
@pytest.mark.parametrize("consumer", ("indexing", "retrieval", "citation", "question_packet"))
def test_only_ready_revision_enters_each_new_trusted_flow(state, consumer) -> None:
    assert can_consume_new_trusted_revision(state, consumer) is (
        state is SourceRevisionState.READY
    )
```

The activation suite must also assert every omitted transition is rejected and
that stale/retired records can still be fetched by audit/reference queries.

## Evidence hash and provider/Ask mapping

`EvidenceUnit.content_sha256` is exactly the 64-character lowercase hexadecimal
result of Task 1.1's `sha256_text(normalized_text)`. The proposed v2 schema
must use `^[0-9a-f]{64}$`; the repository/import boundary must compare it with
the normalized-text digest rather than trusting a caller-supplied checksum.
The direct `EvidenceUnit` to existing `EvidenceRef` mapping is exact:

```text
evidence_id        -> evidence_id
source_revision_id -> source_revision_id
authority_class    -> authority_class
locator.kind.value -> locator_kind
locator.value      -> locator_value
normalized_text    -> excerpt
"sha256:" + content_sha256 -> checksum
```

The exact proposed provider/Ask mapping round-trip test is:

```python
def test_evidence_unit_maps_to_evidence_ref_and_round_trips() -> None:
    from pydantic import TypeAdapter
    from oms_hub.providers.contracts import EvidenceRef

    text = "Hemophilia A is factor VIII deficiency."
    digest = sha256_text(text)
    unit = EvidenceUnit(
        evidence_id="ev_1",
        source_revision_id="sr_1",
        authority_class=AuthorityClass.COURSE_MATERIAL,
        course_id="heme",
        exam_id="e2",
        lecture_id="l13",
        locator=EvidenceLocator(EvidenceLocatorKind.SLIDE, "5"),
        normalized_text=text,
        content_sha256=digest,
    )
    reference = EvidenceRef(
        evidence_id=unit.evidence_id,
        source_revision_id=unit.source_revision_id,
        authority_class=unit.authority_class,
        locator_kind=unit.locator.kind.value,
        locator_value=unit.locator.value,
        excerpt=unit.normalized_text,
        checksum="sha256:" + unit.content_sha256,
    )
    assert reference.excerpt == unit.normalized_text
    assert reference.checksum == "sha256:" + digest
    assert reference.authority_class is unit.authority_class
    restored = TypeAdapter(EvidenceRef).validate_json(
        TypeAdapter(EvidenceRef).dump_json(reference)
    )
    assert restored == reference
```

A future bounded excerpt is a different integrity object: it needs its own
excerpt-integrity rule and field, not reuse of `content_sha256` for a truncated
or transformed excerpt.

## Compatibility and migration

The v1 snapshot remains byte-for-byte stable and continues to serve existing
provider clients. v2 is an additive namespace; it does not reinterpret v1
instances and no v1 records require backfill for this task. A future activation
may dual-read v1 and v2 during the migration window, create v2 source/evidence
records through the owning repository, and switch consumers only after their
contract tests pass. Rollback is disabling v2 consumers and retaining the
immutable v1 snapshot; no destructive down-migration is proposed.

## Required review before activation

Sol-0 must review the additive exporter/snapshot boundary and shared-schema
activation. Every consuming owner must review its model and field usage before
activation: Sol-1 (repository/backfill), Sol-2 (provider indexing), Sol-3 (Ask
retrieval), Sol-5 (question evidence), and Sol-8 (journal evidence). This
proposal makes no claim that those reviews or activation have occurred.

## Exact proposed tests

1. Preserve the existing `tests/contracts/test_schema_exports.py` byte and
   reproducibility assertions for v1.
2. Add a v2 snapshot test that runs the exporter twice, compares both outputs
   with the committed v2 snapshot, and rejects workspace paths or private
   data.
3. Assert every enum value above, the exact EvidenceUnit required and
   defaulted/omittable field sets, the literal conditional schema above,
   UTC date-time constraints, ordinary default emission, authority matching,
   the complete revision-state transition/eligibility matrix, the lowercase
   content-hash rule, provider/Ask mapping, and exclusion of the two derived
   properties.
4. Round-trip one hand-derived instance of every v2 model through its
   `TypeAdapter` and assert that enum values serialize as their strings.
5. Run the provider contract, knowledge, and safe broad Python lanes before
   activation; require Sol-0 and all consuming-owner reviews listed above.

Non-blocking activation recommendations: use a real JSON-Schema instance
validator if one is already available at activation, and have Sol-2 explicitly
review the numeric `source_priority` ranking direction before enabling provider
ranking. Neither recommendation adds a dependency in this proposal.
