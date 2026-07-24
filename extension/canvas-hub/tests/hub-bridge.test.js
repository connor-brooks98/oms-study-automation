import assert from "node:assert/strict";
import test from "node:test";

import {createHubBridge} from "../lib/hub-bridge.js";

const REQUEST_ID = "84729a54-a9a7-4835-9535-e44f8bbcb375";

test("Hub bridge forwards only exact local test events", async () => {
  const sent = [];
  const bridge = createHubBridge({
    origin: "http://127.0.0.1:8765",
    send: async (message) => {
      sent.push(message);
      return {status: "accepted"};
    },
  });

  const accepted = await bridge({
    type: "oms-study-hub:panopto-test",
    detail: {request_id: REQUEST_ID},
  });

  assert.equal(accepted, true);
  assert.deepEqual(sent, [{
    type: "panopto-request-now",
    request_id: REQUEST_ID,
  }]);
});

test("Hub bridge reports when the service worker does not accept the request", async () => {
  const bridge = createHubBridge({
    origin: "http://127.0.0.1:8765",
    send: async () => ({status: "error"}),
  });

  assert.equal(await bridge({
    type: "oms-study-hub:panopto-test",
    detail: {request_id: REQUEST_ID},
  }), false);
});

test("Hub bridge ignores wrong origins, event types, and request IDs", async () => {
  const sent = [];
  for (const value of [
    {origin: "http://localhost:8765", type: "oms-study-hub:panopto-test", id: REQUEST_ID},
    {origin: "http://127.0.0.1:8765", type: "other", id: REQUEST_ID},
    {origin: "http://127.0.0.1:8765", type: "oms-study-hub:panopto-test", id: "../bad"},
  ]) {
    const bridge = createHubBridge({
      origin: value.origin,
      send: async (message) => sent.push(message),
    });
    await bridge({type: value.type, detail: {request_id: value.id}});
  }
  assert.deepEqual(sent, []);
});
