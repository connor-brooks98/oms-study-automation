const HUB = "http://127.0.0.1:8765";
const TOKEN_KEY = "canvasHubBearer";

async function token() {
  return (await chrome.storage.local.get(TOKEN_KEY))[TOKEN_KEY] || null;
}

async function request(path, options = {}, authenticated = true, allowNoContent = false) {
  const headers = {"content-type": "application/json", ...(options.headers || {})};
  if (authenticated) {
    const bearer = await token();
    if (!bearer) throw new Error("Canvas companion is not paired");
    headers.authorization = `Bearer ${bearer}`;
  }
  const response = await fetch(`${HUB}${path}`, {...options, headers, signal: AbortSignal.timeout(15000)});
  if (allowNoContent && response.status === 204) return null;
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(`OMS Study Hub returned HTTP ${response.status}`);
  return body;
}

export async function pair(code) {
  const body = await request("/api/canvas/pair", {
    method: "POST", body: JSON.stringify({code, extension_id: chrome.runtime.id}),
  }, false);
  await chrome.storage.local.set({[TOKEN_KEY]: body.bearer});
  return true;
}

export function heartbeat(state, details = {}) {
  return request("/api/canvas/heartbeat", {method: "POST", body: JSON.stringify({state, ...details})});
}

export function getConfig() { return request("/api/canvas/config"); }
export function postDiscover(items) {
  return request("/api/canvas/discover", {method: "POST", body: JSON.stringify({items})});
}
export function postDownloadComplete(payload) {
  return request("/api/canvas/download-complete", {method: "POST", body: JSON.stringify(payload)});
}
export function postCourses(courses) {
  return request("/api/canvas/courses", {method: "POST", body: JSON.stringify({courses})});
}

export function getPanoptoCommand() {
  return request("/api/panopto/command", {}, true, true);
}
export function postPanoptoHeartbeat(state, details = {}) {
  return request("/api/panopto/heartbeat", {
    method: "POST", body: JSON.stringify({state, ...details}),
  });
}
export function postPanoptoDiscover(payload) {
  return request("/api/panopto/discover", {
    method: "POST", body: JSON.stringify(payload),
  });
}
export function postPanoptoTranscript(payload) {
  return request("/api/panopto/transcript", {
    method: "POST", body: JSON.stringify(payload),
  });
}
export function postPanoptoResult(payload) {
  return request("/api/panopto/result", {
    method: "POST", body: JSON.stringify(payload),
  });
}

export const panoptoHub = {
  heartbeat: postPanoptoHeartbeat,
  postDiscover: postPanoptoDiscover,
  postTranscript: postPanoptoTranscript,
  postResult: postPanoptoResult,
};

export async function pairingStatus() { return Boolean(await token()); }
