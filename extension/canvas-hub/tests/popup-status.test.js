import assert from "node:assert/strict";
import test from "node:test";

import {formatPanoptoResult} from "../lib/popup-status.js";

test("Panopto popup shows the bounded internal error instead of only error", () => {
  assert.equal(
    formatPanoptoResult({
      status: "error",
      error: "TypeError: command polling failed",
    }),
    "Error: TypeError: command polling failed",
  );
});

test("Panopto popup shows safe command reason codes", () => {
  assert.equal(
    formatPanoptoResult({
      status: "failed",
      reason_code: "page_structure_changed",
    }),
    "Failed: page_structure_changed",
  );
});

test("Panopto popup handles an empty request queue", () => {
  assert.equal(formatPanoptoResult(null), "No Hub request pending");
});
