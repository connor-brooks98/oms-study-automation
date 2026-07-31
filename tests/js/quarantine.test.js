const test = require("node:test");
const assert = require("node:assert/strict");

const quarantine = require("../../src/oms_hub/web/static/quarantine.js");

test("lecture options group by course and exam", () => {
  const options = [
    { value: "1", textContent: "Lecture 1", dataset: { subject: "Neuro", exam: "1" } },
    { value: "2", textContent: "Lecture 2", dataset: { subject: "Neuro", exam: "2" } },
    { value: "3", textContent: "Lecture 1", dataset: { subject: "Cardio", exam: "1" } },
  ];

  const grouped = quarantine.groupedLectures(options);

  assert.deepEqual(grouped.Neuro["1"], [{ id: "1", label: "Lecture 1" }]);
  assert.deepEqual(grouped.Neuro["2"], [{ id: "2", label: "Lecture 2" }]);
  assert.deepEqual(grouped.Cardio["1"], [{ id: "3", label: "Lecture 1" }]);
});
