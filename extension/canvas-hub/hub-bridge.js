const bridgePromise = import(chrome.runtime.getURL("lib/hub-bridge.js"));

bridgePromise.then(({createHubBridge}) => {
  const bridge = createHubBridge({
    origin: location.origin,
    send: (message) => chrome.runtime.sendMessage(message),
  });
  window.addEventListener("oms-study-hub:panopto-test", (event) => {
    bridge(event).catch(() => {});
  });
}).catch(() => {});
