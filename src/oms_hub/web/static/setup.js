const TEST_EVENT = "oms-study-hub:panopto-test";
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
    window.dispatchEvent(new CustomEvent(TEST_EVENT, {
      detail: {request_id: body.request_id},
    }));
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

document.querySelectorAll("[data-panopto-test]").forEach((button) => {
  button.addEventListener("click", () => testPanopto(button));
});
connectEvents();
