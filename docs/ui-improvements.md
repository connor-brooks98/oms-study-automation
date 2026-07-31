# Study Hub UI improvement tracker

Status key: `idea` → `spec'd` → `in progress` → `done`

All reviewed items from the July 31 tracker are implemented.

- [x] **1. Previous/next lecture navigation — done.** Navigation crosses exam boundaries within the same course, labels the destination exam/lecture, and supports bracket shortcuts outside form controls.
- [x] **2. Unified upload dropzone — done.** One upload page accepts mixed PPTX/TXT selections, partitions them into the existing backend flows, and presents combined status. Legacy PPT remains intentionally unsupported because the pipeline does not accept it.
- [x] **3. Status-color accessibility — done.** Statuses now include shape/pattern distinctions as well as text and color.
- [x] **4. Narrow lecture rows — done.** File availability is folded into the status marker and the compact layout no longer needs parallel file-flag columns.
- [x] **5. Generation action distinction — done.** Generate actions have a distinct treatment from navigation/download links.
- [x] **6. Missing-file visual cue — done.** Missing cards no longer use the dashed drop-target convention.
- [x] **7. Settings navigation — done.** Settings has in-page anchor navigation for its growing collection of sections.
- [x] **8. Tracker import polish — done.** Tracker import uses the shared dropzone/card treatment.
- [x] **9. Tracker before/after count — done.** Preview shows current and resulting catalog totals.
- [x] **10. Quarantine hierarchy — done.** Assignment follows course → exam → lecture controls rather than one flat list.
- [x] **11. Quarantine batch handling — done.** Multiple assignments can be submitted atomically without a full reload per item.
- [x] **12. Shared design tokens — done.** Internal and public quiz styles consume one shared token stylesheet.
- [x] **13. Reset confirmation — done.** Public quiz progress reset requires confirmation.
- [x] **14. Answer correctness accessibility — done.** Choices carry visible correctness labels in addition to color/border changes.

The previously listed `anki.html` and `artifact_text.html` entries were review reminders, not change requests. Their behavior was preserved.
