import test from "node:test";
import assert from "node:assert/strict";
import {extractFileIds} from "../lib/discovery.js";

test("extracts only same-origin Canvas download links", () => {
  const html = `<a href="/courses/751/files/30/download">PPTX</a><a href="https://evil.example/files/31/download">bad</a>`;
  assert.deepEqual(extractFileIds(html), ["30"]);
});

test("ignores arbitrary links and duplicate file links", () => {
  const html = `<a href="/assignments/1">assignment</a><a href="/files/30/download">one</a><a href="/files/30/download">two</a>`;
  assert.deepEqual(extractFileIds(html), ["30"]);
});
