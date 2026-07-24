const TEST_EVENT = "oms-study-hub:panopto-test";
const ACK_EVENT = "oms-study-hub:panopto-request-ack";
let polling = null;

function label(value) {
  return String(value || "unknown").replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function statusClass(value) {
  if (["connected", "configured", "approved", "complete"].includes(value)) return "complete";
  if (["failed", "error", "not_configured", "not_readable"].includes(value)) return "failed";
  if (["awaiting_login", "waiting_for_captions", "changed"].includes(value)) return "needs_review";
  return "";
}

function updateCard(name, state, meta) {
  const card = document.querySelector(`[data-service="${name}"]`);
  if (!card) return;
  const badge = card.querySelector("[data-status]");
  badge.textContent = label(state);
  badge.className = `status ${statusClass(state)}`.trim();
  if (meta) card.querySelector("[data-meta]").textContent = meta;
}

function render(snapshot) {
  updateCard(
    "canvas",
    snapshot.canvas.state,
    snapshot.canvas.automatic ? "Automatic processing on" : "Automatic processing paused",
  );
  updateCard(
    "panopto",
    snapshot.panopto.request_state || snapshot.panopto.state,
    label(snapshot.panopto.progress || (snapshot.panopto.tested ? "Connection tested" : "Ready for a connection test")),
  );
  updateCard("openai", snapshot.openai.state);
  updateCard("prompt", snapshot.prompt.state, snapshot.prompt.path);
}

async function pollStatus() {
  const response = await fetch("/api/setup/status", {headers: {accept: "application/json"}});
  if (response.ok) render(await response.json());
}

function startPolling() {
  if (polling) return;
  pollStatus().catch(() => {});
  polling = setInterval(() => pollStatus().catch(() => {}), 5000);
}

function connectEvents() {
  const events = new EventSource("/api/setup/events");
  events.addEventListener("status", (event) => {
    render(JSON.parse(event.data));
    if (polling) {
      clearInterval(polling);
      polling = null;
    }
  });
  events.onerror = () => {
    events.close();
    startPolling();
    setTimeout(connectEvents, 15000);
  };
}

function launchInChrome(requestId) {
  return new Promise((resolve, reject) => {
    let timeout;
    function finish(error) {
      clearTimeout(timeout);
      window.removeEventListener(ACK_EVENT, acknowledge);
      if (error) reject(error);
      else resolve();
    }
    function acknowledge(event) {
      if (event?.detail?.request_id !== requestId) return;
      if (event.detail.accepted === true) finish();
      else finish(new Error("The extension did not accept the Panopto request."));
    }
    window.addEventListener(ACK_EVENT, acknowledge);
    timeout = setTimeout(
      () => finish(new Error(
        "The extension did not receive the request. Reload this Setup page after reloading the extension.",
      )),
      3000,
    );
    window.dispatchEvent(new CustomEvent(TEST_EVENT, {
      detail: {request_id: requestId},
    }));
  });
}

async function testPanopto(button) {
  const feedback = document.querySelector("[data-panopto-feedback]");
  document.querySelectorAll("[data-panopto-test]").forEach((item) => {
    item.disabled = true;
  });
  if (feedback) feedback.textContent = "Opening the latest shared recording…";
  try {
    const response = await fetch("/setup/panopto/test", {
      method: "POST",
      headers: {accept: "application/json"},
    });
    if (!response.ok) throw new Error("The Hub could not start the test");
    const body = await response.json();
    await launchInChrome(body.request_id);
    if (feedback) feedback.textContent = "Panopto test launched in Chrome.";
  } catch (error) {
    if (feedback) feedback.textContent = error.message;
  } finally {
    button.disabled = false;
    document.querySelectorAll("[data-panopto-test]").forEach((item) => {
      item.disabled = false;
    });
  }
}

async function scanPanopto(button) {
  const feedback = document.querySelector("[data-panopto-scan-feedback]");
  button.disabled = true;
  if (feedback) feedback.textContent = "Opening Shared with Me…";
  try {
    const response = await fetch("/setup/panopto/scan", {
      method: "POST",
      headers: {accept: "application/json"},
    });
    if (!response.ok) throw new Error("The Hub could not start the scan");
    const body = await response.json();
    await launchInChrome(body.request_id);
    if (feedback) feedback.textContent = "Panopto scan launched in Chrome.";
  } catch (error) {
    if (feedback) feedback.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

document.querySelectorAll("[data-panopto-test]").forEach((button) => {
  button.addEventListener("click", () => testPanopto(button));
});
document.querySelectorAll("[data-panopto-scan]").forEach((button) => {
  button.addEventListener("click", () => scanPanopto(button));
});
connectEvents();
