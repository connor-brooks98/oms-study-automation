const bridgePromise = import(chrome.runtime.getURL("lib/hub-bridge.js"));
const ACK_EVENT = "oms-study-hub:panopto-request-ack";

bridgePromise.then(({createHubBridge}) => {
  const bridge = createHubBridge({
    origin: location.origin,
    send: (message) => chrome.runtime.sendMessage(message),
  });
  window.addEventListener("oms-study-hub:panopto-test", (event) => {
    const requestId = event?.detail?.request_id;
    bridge(event)
      .then((accepted) => {
        window.dispatchEvent(new CustomEvent(ACK_EVENT, {
          detail: {request_id: requestId, accepted},
        }));
      })
      .catch(() => {
        window.dispatchEvent(new CustomEvent(ACK_EVENT, {
          detail: {request_id: requestId, accepted: false},
        }));
      });
  });
}).catch(() => {});
