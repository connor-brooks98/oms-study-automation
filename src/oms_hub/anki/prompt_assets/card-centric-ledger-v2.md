---
id: card-centric-ledger-v2
version: 2.1.7
max_tokens: 7000
response_format: json
schema: lcl_v2
---

# Card-centric v2 coverage checklist

## Quality-first deck policy

Optimize for the smallest set of the best-supported, highest-yield,
nonredundant cards. Treat 60 as a warning floor, 65 as the ordinary target,
and 70 as a soft cap; these are never quotas and the ledger must never pad
coverage merely to reach a count. Do not omit a unique, grounded, high-value
fact solely because the ordinary target has been reached.

The selected provider/model route determines whether a temperature control is
transported; do not infer an explicit temperature or model route from this
prompt metadata. The runtime route and audited generation parameters capture
the selected model.

Use only supplied lecture passages. Return the v2 ledger contract with stable
sequential concept IDs. Each concept MUST include `suggested_fact_count` (1-5),
exactly that many nonblank `fact_descriptions`, one optional forbidden-target
array per fact in `forbidden_cloze_targets_by_fact`, and `is_mechanism`.
`canonical_statement` remains the single canonical concept statement. Preserve
the legacy top-level `forbidden_cloze_targets` for compatibility. Never invent
source IDs or material absent from the lecture.

For an expression-location fact, put only the named molecule in that fact's
`forbidden_cloze_targets_by_fact` row. Never forbid the location, because the
gap card must leave the molecule visible and test where it is expressed.

The user input may include `depth_control_evidence`. These are bounded lecture
passages supplied only to substantiate named checklists or warning signs from
the summary depth map. Match the lecturer's demonstrated depth: preserve every
source-listed item and threshold only when the lecture teaches the list for
recall. Awareness language such as "know these exist" or "keep these in mind"
requires one concrete recognition fact naming the list and its clinical purpose,
not item-by-item recall. An evidence item marked `representation` =
`awareness_only` makes this one-fact, no-enumeration rule mandatory.

Every `fact_description` must be one independently testable medical proposition.
Do not join independently testable clauses with a semicolon; emit separate facts
instead. An explicit comparison emphasized by the lecturer may remain one fact.
Never emit a fact that merely describes lecture depth, coverage, emphasis, or
pedagogy. A coverage-control sentence that names diseases or topics is still an
inclusion instruction: emit at least one concrete medical concept for every name
at its stated depth, even though the control sentence itself is not a fact. Do not
lose a named entity when replacing a depth summary. Keep depth only in the
concept's `depth` field. When one entity requires
more than five distinct facts, emit a continuation concept with the same
`primary_entity` and the next sequential concept ID; never rebundle the extra
facts into composite statements. Do not repeat or recombine a fact across
continuation concepts.

Surface depth still requires one or two concrete identification propositions:
name the actual gene/defect, characteristic exposure, or clinical recognition
pattern supported by the source. Never emit phrases such as "covered at a basic
level," "a basic gene defect," "a characteristic clinical presentation,"
"associated gene and clinical features," or another promise to teach details
that the fact itself omits.

A named checklist or warning-sign list taught for recall is one recognition fact
whose `fact_description` preserves the concrete source-listed items and
thresholds. The gap-card stage can split that grounded list fact into a small
ordered card set. When the lecturer teaches only awareness, emit one fact naming
the specific list and what clinical decision it supports; do not enumerate it.

Placeholder statements are invalid. Do not say only that a checklist is used to
recognize disease, or that an entity has a characteristic gene and presentation
as noted in the lecture. Name the concrete checklist items, gene/defect, exposure,
and clinical pattern in the fact itself.

Create a standalone diagnostic-workflow concept only when a supplied passage
teaches that workflow as a workflow. Tests mentioned separately inside disease
sections remain facts of those disease concepts.

## Importance is derived, never estimated

Set `importance` exactly from `depth` and `emphasis_flag`; do not use any other
meaning of importance:

- `high` **if and only if** `depth` is `deep` **or** `emphasis_flag` is `true`.
- `medium` **if and only if** `emphasis_flag` is `false` and `depth` is `medium`.
- `low` **if and only if** `emphasis_flag` is `false` and `depth` is `surface`.

Examples: `deep` + `false` is `high`; `surface` + `true` is `high`; `medium` +
`false` is `medium`; and `surface` + `false` is `low`. A conflict is invalid.
