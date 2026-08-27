# Lecture 101 Jeffrey Modell depth correction

Date: 2026-08-27  
Status: independently approved; implementation complete locally  
Scope: Lecture 101 ledger/card depth only; no production or Anki mutation

## New evidence

The user supplied `/Users/connor/Downloads/Lec_23 Example.txt`, a 69-card deck
created from the same lecture resources in a Claude Project. It contains exactly
one row mentioning Jeffrey Modell:

> Among the Jeffrey Modell Foundation's 10 warning signs of immunodeficiency, a
> family history of primary immunodeficiency is considered the single most
> important clue. Other signs include at least two pneumonias per year and at
> least two months on antibiotics with little effect.

The deck is comparison evidence, not an authoritative source. In particular,
the supplied lecture slide lists ten coequal signs and does not identify family
history as the single most important one, so that priority claim must not be
copied unless another supplied lecture passage entails it.

The primary lecture evidence says that the list exists and tells students to
"keep these in mind." The user confirms that the lecturer's intended depth was
awareness only: know that the Jeffrey Modell parameters exist, not memorize all
ten thresholds.

## Finding

The current acceptance-specific completeness rule is wrong for the intended
lecture depth. Requiring all ten items in one ledger fact, then splitting that
fact into list cards, overweights a reference slide and contradicts both the
spoken emphasis and the independently generated comparison deck.

The generic safety invariants remain correct: facts must be source-entailable,
atomic, auditable, and no unrelated concept may change during targeted repair.
Only the Jeffrey Modell content/depth requirement changes.

## Revised implementation plan

1. Remove the Lecture-101-specific `_JEFFREY_MODELL_ITEMS` validator and its
   tests. Do not replace it with another disease-specific validator.
2. Change the ledger prompt so a named list is enumerated only when the lecture
   explicitly teaches its items for recall. Awareness language such as "know
   these exist" or "keep these in mind" permits one recognition fact naming the
   tool and its purpose without reproducing every item.
3. For Lecture 101, accept one source-grounded awareness fact equivalent to:
   "The Jeffrey Modell Foundation's 10 warning signs are a pediatric screening
   aid for recognizing when to consider an inborn error of immunity."
4. Do not assert that family history is the single most important clue because
   the captured lecture evidence does not establish that ranking.
5. Keep the bounded Jeffrey source passages available to the provider for
   grounding, but do not make the presence of all ten items a validation
   requirement.
6. Retain the already approved targeted-repair mechanics unchanged: one repair
   call, replacements/additions only, scope cap, byte-identical untouched
   concepts, complete layered defects, full merged-ledger validation, and a
   persisted merge manifest.
7. Add focused tests proving that an awareness-level Jeffrey fact passes, a
   depth/pedagogy meta statement still fails, and no ten-item expansion is
   required. Then run all Anki tests and Ruff before isolated acceptance.
8. Rerun Lecture 101 only on NUC staging port 8788 with the existing Voyage
   generation. Do not restart production port 8765 and do not apply cards.

## Acceptance change

Replace the earlier requirement that Jeffrey Modell be represented completely
through split list cards with this requirement:

- exactly one compact, source-grounded recognition card is sufficient for the
  Jeffrey Modell warning signs in Lecture 101;
- the card must establish the list's existence and clinical recognition purpose;
- it must not require recall of all ten thresholds or introduce an unsupported
  ranking among them.

## Requested reviewer decision

Return `APPROVE` if this amendment correctly follows the lecture's demonstrated
depth while preserving the approved fail-closed targeted-repair design. Return
`BLOCK` only with concrete required changes. Do not implement, deploy, restart
production, or apply cards during review.

## Reviewer outcome

`VERDICT: APPROVE` (2026-08-27). The reviewer agreed that user-confirmed
awareness depth supersedes the earlier ten-threshold memorization requirement
and that the generic fail-closed repair invariants remain intact.
