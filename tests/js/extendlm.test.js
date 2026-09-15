const test = require("node:test");
const assert = require("node:assert/strict");
const { queueExtendLMFiles } = require("../../src/oms_hub/web/static/extendlm.js");

test("bulk files use separate requests with the fixed destination", async () => {
  const files = [new File(["slides"], "lecture.pdf"), new File(["transcript"], "lecture.txt")];
  const seen = [];
  let receipts = 0;
  await queueExtendLMFiles(async (path, body) => {
    assert.equal(path, "/uploads");
    assert.equal(body.get("notebook_id"), "selected-notebook");
    assert.equal(body.getAll("files").length, 1);
    seen.push(body.get("files").name);
  }, files, "selected-notebook", async () => { receipts++; });
  assert.deepEqual(seen, ["lecture.pdf", "lecture.txt"]);
  assert.equal(receipts, 2);
});

test("an interrupted batch stops without silently retrying a submitted file", async () => {
  const files = [new File(["a"], "a.txt"), new File(["b"], "b.txt"), new File(["c"], "c.txt")];
  let sent = 0, receipts = 0;
  await assert.rejects(queueExtendLMFiles(async () => {
    if (++sent === 2) throw new Error("connection interrupted");
  }, files, "notebook", async () => { receipts++; }), /connection interrupted/);
  assert.equal(sent, 2);
  assert.equal(receipts, 1);
});
