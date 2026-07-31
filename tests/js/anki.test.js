const test = require("node:test");
const assert = require("node:assert/strict");

const anki = require("../../src/oms_hub/web/static/anki.js");

const lectures = [{
  id: 42,
  subject: "Heme Lymph",
  exam_number: 1,
  lecture_number: 4,
  topic: 'Anemia "I"',
  target_tag:
    "AnkiHub_Optional::LMU_OMS_II::HemeLymph::Block1::Lec4_Anemia_I",
  revisions: [{
    id: 7,
    kind: "slides",
    source_sha256: "a".repeat(64),
  }],
}];

test("lecture payload preserves quoted topics and current revisions", () => {
  const parsed = anki.parseLecturePayload(JSON.stringify(lectures));

  assert.deepEqual(parsed, lectures);
});

test("lecture selection resolves sources and editable tag by numeric id", () => {
  const selected = anki.resolveLecture(lectures, "42");

  assert.equal(selected.id, 42);
  assert.equal(selected.revisions[0].kind, "slides");
  assert.equal(
    selected.target_tag,
    "AnkiHub_Optional::LMU_OMS_II::HemeLymph::Block1::Lec4_Anemia_I",
  );
  assert.equal(anki.resolveLecture(lectures, "999"), null);
});
