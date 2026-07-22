import test from "node:test";
import assert from "node:assert/strict";
import {runScan} from "../lib/scanner.js";

test("scanner discovers configured courses and downloads approved items", async () => {
  const states = [];
  const downloads = [];
  const client = {
    heartbeat: async (state) => { states.push(state); },
    getConfig: async () => ({courses: [{course_id: "751", enabled: true}]}),
    postDiscover: async () => ({dispositions: [{action: "download", source_item_id: 1, relative_filename: "1/2/a.pptx"}]}),
  };
  const result = await runScan({hub: client, discoverCourse: async () => [{download_url: "url"}], downloadDisposition: async (d) => downloads.push(d)});
  assert.equal(result.status, "complete");
  assert.deepEqual(states, ["scanning", "connected"]);
  assert.equal(downloads.length, 1);
});
