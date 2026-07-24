import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";

const background = readFileSync(
  new URL("../background.js", import.meta.url),
  "utf8",
);

test("service worker uses no dynamic imports", () => {
  assert.doesNotMatch(background, /\bimport\s*\(/);
});
