# Pass Resource Catalog Design

**Approved:** 2026-08-30

## Goal

Let a learner choose **Other** for a lecture pass, type the resource name, use it for that pass, and retain it as a reusable option on every lecture.

## User experience

- The existing default resource order remains: Lecture, Anki, Lecture outline, Practice questions, then Other.
- Selecting Other does not save the literal word `Other`. It reveals an inline, labeled Resource name field and an Add & use button in that pass row.
- The field receives focus, is required, and accepts at most 100 characters.
- Add & use trims surrounding whitespace and sends the typed name through the existing CSRF-protected pass PATCH.
- A successful save selects the canonical saved name for the current pass, adds it to every pass-resource dropdown on the page, hides and clears the editor, and announces success through the existing live region.
- A blank value is rejected without a request. A failed request preserves the previously saved resource and leaves the editor available for correction.
- Selecting a normal resource while the editor is open hides and clears it, then follows the existing save behavior.
- The reveal uses the app's existing motion class and global reduced-motion behavior.

## Persistence and API

- Add schema v31 table `lecture_pass_resources` with `id`, a case-insensitive unique `name` limited to 100 characters, and `created_at`.
- Seed the four defaults in their current display order and backfill distinct nonblank values already recorded on lecture passes, excluding the Other sentinel.
- The resource catalog is global across all courses and lectures.
- Keep the existing `PATCH /api/lectures/{lecture_id}/passes/{position}` request shape. When it receives a nonblank resource other than Other, the repository inserts it into the catalog idempotently and updates the pass in the same transaction.
- Case-insensitive duplicates reuse the first stored spelling, so `Pathoma` and `pathoma` produce one dropdown option.
- Clearing a pass resource does not remove the catalog entry. Custom names therefore remain available for future use even after the original pass changes.
- Lecture rendering obtains its dropdown values from the catalog. No new endpoint is required.

## Boundaries

- Do not add rename, delete, reorder, or standalone resource-management UI.
- Do not add dependencies.
- Preserve existing pass completion dates, extra-pass behavior, exam overview behavior, and concurrent pass-update safety.
- Continue escaping server-rendered names and construct browser options with DOM properties rather than HTML strings.
- Do not push, deploy, or mutate an external Study Hub runtime.
