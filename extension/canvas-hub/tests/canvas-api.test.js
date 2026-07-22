import test from "node:test";
import assert from "node:assert/strict";
import {isAuthenticationResponse, listAll} from "../lib/canvas-api.js";

test("detects Canvas login HTML", () => {
  const response = {status: 200, headers: new Headers({"content-type": "text/html"}), url: "https://lmunet.instructure.com/login"};
  assert.equal(isAuthenticationResponse(response, "<form id='login_form'>"), true);
});

test("follows Canvas Link pagination", async () => {
  const pages = [
    new Response(JSON.stringify([{id: 1}]), {headers: {"content-type": "application/json", "link": "</api/v1/x?page=2>; rel=\"next\""}}),
    new Response(JSON.stringify([{id: 2}]), {headers: {"content-type": "application/json"}}),
  ];
  assert.deepEqual(await listAll("/api/v1/x", async () => pages.shift()), [{id: 1}, {id: 2}]);
});
