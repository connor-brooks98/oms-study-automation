---
id: card-house-rules
version: 2.0.0
shared: true
---

## Card rules

- Max 2 cloze deletions per card. Hard limit.
- One sentence in the Text field. No compound sentences.
- Blank targets: mechanism, association, consequence, distinguishing feature.
- No enumeration cards. Split lists into individual mechanism cards.
- No epidemiologic rates or incidence numbers as cloze targets. Definitional
  percentages are fine (MBC = 99.9%); population statistics are not.
- Extra field: one sentence of clinical context or mechanism. One sentence
  maximum. No bullets, no multi-part lists.
- If a concept needs an image, write the card with
  `[IMAGE NEEDED: description]` in Extra and set `image_needed`.

## Context trap

A context trap is a cloze whose answer is trivially inferable from the lecture
topic alone.

- Never blank any string in `forbidden_cloze_targets[]`.
- When `lecture_entity_count == 1`, the concept's primary entity is also
  forbidden — blanking *Staph aureus* in the Staph lecture teaches nothing.
- When `lecture_entity_count > 1`, blanking a disease name is permitted where
  it is the discriminating feature being tested. In a lecture covering six
  hemolytic anemias, "which one is X-linked recessive" is a real question.

## Formatting convention

1. Disease/organism name in the stem → plain `<b>bold</b>`
2. Cloze answer content → `<b>bold inside</b>` the cloze brackets
3. Answer content spilling past the closing bracket → also bold
4. Contextual structural nouns (receptor, enzyme, route, test) → `<u>underline</u>`
5. Never bold or underline filler words — articles, prepositions, conjunctions

**Good:**
```
{{c1::<b>Protein A</b>}} binds the Fc <u>region</u> of IgG to {{c2::<b>prevent opsonization</b>}}
<b>C. diphtheriae</b> toxin inhibits protein synthesis by {{c1::<b>ADP-ribosylating EF-2</b>}}
```

**Bad:**
```
{{c1::<b>Staph aureus</b>}} produces Protein A                    ← context trap
<b>Staph aureus</b> virulence factors: {{c1::}}, {{c2::}}, {{c3::}}  ← enumeration
{{c1::<b>30%</b>}} of the population are nasal carriers            ← epidemiology stat
```

## Check before finalizing each card

- Disease/organism name bolded in stem?
- Every cloze answer wrapped in `<b>` inside the brackets?
- Contextual label nouns underlined, not bolded?
- One sentence in Text, one sentence in Extra?
- Cloze target absent from `forbidden_cloze_targets[]`?

## Note type

`AnKingOverhaul (AnKing Step Deck / AnKingMed)` — no exceptions.
Cloze syntax `{{c1::answer}}` in the `Text` field, context in `Back Extra`.
