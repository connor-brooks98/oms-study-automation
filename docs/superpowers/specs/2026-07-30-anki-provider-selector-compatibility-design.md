# Anki Provider Compatibility and Pathway Selector Design

## Goal

Make the first live Anki curation run compatible with Anthropic structured
outputs and replace the nested lecture accordion with three equal, dependent
Course → Exam → Lecture dropdowns.

The visual state reference is
[Study Hub — Anki Cascading Lecture Selector](https://www.figma.com/design/05mE9utL7fDYZcA8f83vHO).

## Provider compatibility

The Anki pipeline continues to use Pydantic's complete JSON Schema for local
response validation. Before an Anthropic structured-output request, the
Anthropic adapter creates a provider-safe copy of that schema:

- recursively remove unsupported validation keywords such as `minLength`,
  `maxLength`, `minimum`, `maximum`, and array-length constraints;
- convert fixed positional tuple schemas expressed as `prefixItems` into a
  homogeneous `items` schema when all tuple entries are equivalent;
- retain structural keywords including `type`, `properties`, `required`,
  `additionalProperties`, `$defs`, `$ref`, `items`, `enum`, and `const`;
- never mutate the caller's original schema.

The returned JSON is still validated against the original Pydantic model, so
provider compatibility does not weaken Study Hub's data contract.

The Anki page defaults to the active provider and its saved model from
`LLMSettingsRepository`. It also receives the saved model for every configured
provider. Changing the provider in the form updates the editable Model field to
that provider's saved model.

Provider diagnostics remain secret-safe. Generic HTTP 400 responses are
reported as invalid provider requests rather than always claiming that the
model is invalid. HTTP 404 remains the model-not-found classification. Invalid
provider requests are permanent failures and are not automatically retried.

## Cascading lecture selector

The selector is one responsive row containing three equally sized labeled
native select controls:

1. **Course** is enabled when the page loads and lists each catalog course once.
2. **Exam** is disabled until Course is selected. It then lists only exams in
   that course as `Exam N`.
3. **Lecture** is disabled until Exam is selected. It then lists only lectures
   in that exact course and exam as `Lecture N — Topic`.

Changing Course clears Exam, Lecture, the hidden lecture ID, rendered source
choices, the selected-lecture summary, and the generated lecture tag. Changing
Exam clears the same lecture-dependent values. Selecting Lecture sets the
hidden numeric lecture ID, renders its current slides/transcript revisions
checked by default, and fills the actual editable canonical lecture tag.

Disabled controls use the browser's disabled semantics and a visibly grey
surface. Below 720 pixels the three controls stack in Course → Exam → Lecture
order.

## Data and safety

The existing quote-safe `application/json` lecture payload remains the single
client-side source of catalog data. The curation-job request, database schema,
source revision selection, canonical tag format, and Anki apply safeguards do
not change.

No Anki collection, index, ingestion record, or acceptance-copy data is
modified by this patch.

## Testing

- Anthropic adapter tests prove unsupported schema keywords are absent from the
  sent request, structural schema content remains, fixed tuples are normalized,
  and the input schema is unchanged.
- Provider diagnostic tests prove HTTP 400 no longer produces the misleading
  model-specific message while HTTP 404 still does, and invalid requests are
  not retried.
- Route tests prove the page defaults to the active saved provider/model and
  exposes all saved provider models.
- JavaScript tests prove Course, Exam, and Lecture option derivation and
  dependent resets.
- Template tests prove the three labeled selects exist in the correct enabled
  state and the removed accordion markup does not return.
- The complete Python, JavaScript, lint, type, and whitespace gates must pass
  before the branch is pushed.
