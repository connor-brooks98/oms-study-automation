const test = require('node:test');
const assert = require('node:assert/strict');
const { filterRows, PAGE_SIZE } = require('../../src/oms_hub/web/static/lecture_picker.js');
const rows = [
  { id: 71, course: 'Neuro', exam: 2, label: 'Lecture 07 — Reflex arcs · slides' },
  { id: 82, course: 'Neuro', exam: 3, label: 'Lecture 08 — Reflex arcs · transcripts' },
  { id: 93, course: 'Heme', exam: 2, label: 'Lecture 07 — Iron · slides' },
];
test('course and exam scope combine with multiword case-insensitive search without changing source IDs', () => {
  assert.deepEqual(filterRows(rows, 'Neuro', '2', 'REFLEX slides').map(row => row.id), [71]);
  assert.deepEqual(filterRows(rows, '', '2', '07').map(row => row.id), [71, 93]);
  assert.deepEqual(filterRows(rows, 'Heme', '', 'reflex'), []);
  assert.deepEqual(filterRows(rows, '', '', '  '), rows);
  assert.equal(PAGE_SIZE, 20);
});
