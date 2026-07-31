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
}, {
  id: 55,
  subject: "Heme Lymph",
  exam_number: 3,
  lecture_number: 22,
  topic: "Hemolytic Anemias",
  target_tag:
    "AnkiHub_Optional::LMU_OMS_II::HemeLymph::Block3::Lec22_Hemolytic_Anemias",
  revisions: [],
}, {
  id: 77,
  subject: "Neuro",
  exam_number: 2,
  lecture_number: 8,
  topic: "Brainstem",
  target_tag:
    "AnkiHub_Optional::LMU_OMS_II::Neuro::Block2::Lec8_Brainstem",
  revisions: [],
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

test("dependent selector options stay inside the selected pathway", () => {
  assert.deepEqual(anki.courseOptions(lectures), ["Heme Lymph", "Neuro"]);
  assert.deepEqual(anki.examOptions(lectures, "Heme Lymph"), [1, 3]);
  assert.deepEqual(
    anki.lectureOptions(lectures, "Heme Lymph", "3").map((item) => item.id),
    [55],
  );
  assert.deepEqual(anki.examOptions(lectures, "Unknown"), []);
  assert.deepEqual(anki.lectureOptions(lectures, "Heme Lymph", ""), []);
});

test("provider changes resolve that provider's saved model", () => {
  const models = {
    anthropic: "claude-sonnet-4-6",
    openai: "gpt-5.2",
  };

  assert.equal(
    anki.resolveProviderModel(models, "anthropic"),
    "claude-sonnet-4-6",
  );
  assert.equal(anki.resolveProviderModel(models, "gemini"), "");
});

test("only failed curation jobs expose pipeline retry", () => {
  assert.equal(anki.canRetryCuration("failed"), true);
  assert.equal(anki.canRetryCuration("judging_pass_1"), false);
  assert.equal(anki.canRetryCuration("complete"), false);
});
