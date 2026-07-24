const REQUEST_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const HUB_ORIGIN = "http://127.0.0.1:8765";
const TEST_EVENT = "oms-study-hub:panopto-test";

export function createHubBridge({origin, send}) {
  return async function forwardPanoptoTest(event) {
    const requestId = event?.detail?.request_id;
    if (
      origin !== HUB_ORIGIN
      || event?.type !== TEST_EVENT
      || typeof requestId !== "string"
      || !REQUEST_ID.test(requestId)
    ) {
      return false;
    }
    await send({
      type: "panopto-request-now",
      request_id: requestId,
    });
    return true;
  };
}
