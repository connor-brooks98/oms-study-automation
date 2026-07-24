import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";

const background = readFileSync(
  new URL("../background.js", import.meta.url),
  "utf8",
);
const panoptoContent = readFileSync(
  new URL("../panopto-content.js", import.meta.url),
  "utf8",
);

test("service worker uses no dynamic imports", () => {
  assert.doesNotMatch(background, /\bimport\s*\(/);
});

test("Panopto content script downloads captions instead of scraping lines", () => {
  assert.match(panoptoContent, /panopto:caption-download/);
  assert.doesNotMatch(panoptoContent, /panopto:extract/);
  assert.doesNotMatch(panoptoContent, /readTranscript/);
});

test("immediate Hub requests acknowledge before running Panopto work", () => {
  const immediateHandler = background.match(
    /if \(message\?\.type === "panopto-request-now"\) \{([\s\S]*?)\n  \}/,
  )?.[1] || "";

  assert.match(immediateHandler, /sendResponse\(\{status: "accepted"\}\)/);
  assert.match(immediateHandler, /pollCommands\(\)\.catch/);
  assert.ok(
    immediateHandler.indexOf("sendResponse") < immediateHandler.indexOf("pollCommands"),
  );
});
