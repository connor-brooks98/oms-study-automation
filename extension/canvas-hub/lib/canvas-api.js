const CANVAS_ORIGIN = "https://lmunet.instructure.com";

export class CanvasLoginRequiredError extends Error {}
export class CanvasProtocolError extends Error {}

export function isAuthenticationResponse(response, bodyText) {
  const contentType = response.headers.get("content-type") || "";
  return response.status === 401 || response.status === 403 ||
    new URL(response.url || CANVAS_ORIGIN, CANVAS_ORIGIN).pathname.startsWith("/login") ||
    (contentType.includes("text/html") && /login_form|sign in|log in/i.test(bodyText));
}

export async function canvasFetch(path, fetchImpl = fetch) {
  const url = new URL(path, CANVAS_ORIGIN);
  if (url.origin !== CANVAS_ORIGIN) throw new CanvasProtocolError("Canvas URL left the LMU origin");
  const response = await fetchImpl(url.href, {credentials: "include", redirect: "follow"});
  const text = await response.text();
  if (isAuthenticationResponse(response, text)) throw new CanvasLoginRequiredError("Canvas login required");
  if (!response.ok) throw new CanvasProtocolError(`Canvas returned HTTP ${response.status}`);
  return {response, text};
}

export async function canvasFetchJson(path, fetchImpl = fetch) {
  const {response, text} = await canvasFetch(path, fetchImpl);
  if (!(response.headers.get("content-type") || "").includes("application/json")) {
    throw new CanvasProtocolError("Canvas returned a non-JSON API response");
  }
  return {response, data: JSON.parse(text)};
}

function nextLink(linkHeader) {
  if (!linkHeader) return null;
  const part = linkHeader.split(",").find((value) => /rel="next"/.test(value));
  const match = part && part.match(/<([^>]+)>/);
  if (!match) return null;
  const url = new URL(match[1], CANVAS_ORIGIN);
  if (url.origin !== CANVAS_ORIGIN) throw new CanvasProtocolError("pagination left the LMU origin");
  return url.href;
}

export async function listAll(path, fetchImpl = fetch) {
  const values = [];
  let next = path;
  for (let page = 0; next && page < 100; page += 1) {
    const {response, data} = await canvasFetchJson(next, fetchImpl);
    if (!Array.isArray(data)) throw new CanvasProtocolError("Canvas list response was not an array");
    values.push(...data);
    next = nextLink(response.headers.get("link"));
  }
  if (next) throw new CanvasProtocolError("Canvas pagination exceeded 100 pages");
  return values;
}
