const LIST_CONTAINERS = ["#listViewContainer", "[data-testid='session-list']"];
const LIST_ROWS = ["tbody tr.list-view-row", "[data-session-id]"];
const TITLE_LINKS = [
  ".item-title.title-link a.detail-title",
  "a[href*='/Panopto/Pages/Viewer.aspx?id=']",
];
const TRANSCRIPT_PANES = [
  "div.event-tab-scroll-pane",
  "[data-testid='transcript-scroll-pane']",
];
const TRANSCRIPT_LINES = [
  "li.index-event",
  "[data-testid='transcript-line']",
];

export class PanoptoPageError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "PanoptoPageError";
    this.code = code;
  }
}

function first(root, selectors) {
  for (const selector of selectors) {
    const value = root.querySelector(selector);
    if (value) return value;
  }
  return null;
}

function all(root, selectors) {
  for (const selector of selectors) {
    const values = [...root.querySelectorAll(selector)];
    if (values.length) return values;
  }
  return [];
}

function text(root, selectors) {
  return (first(root, selectors)?.textContent || "").trim();
}

function parseDuration(value) {
  const parts = value.trim().split(":").map(Number);
  if (!parts.length || parts.some((part) => !Number.isFinite(part))) return 0;
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return parts[0];
}

function viewer(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new PanoptoPageError("page_structure_changed", "Invalid viewer link");
  }
  const sessionId = url.searchParams.get("id");
  if (
    url.protocol !== "https:"
    || url.hostname !== "lmunet.hosted.panopto.com"
    || url.pathname !== "/Panopto/Pages/Viewer.aspx"
    || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(sessionId || "")
  ) {
    throw new PanoptoPageError(
      "page_structure_changed",
      "Viewer link is not an LMU Panopto recording",
    );
  }
  return {sessionId, url: url.toString()};
}

export function isLoginRequired(document, location) {
  if (location.hostname === "login.microsoftonline.com") return true;
  return Boolean(
    document.querySelector("form[action*='Login']")
    || document.querySelector("[data-testid='login-page']")
    || document.querySelector("#loginControl"),
  );
}

export function readSharedRecordings(document) {
  const container = first(document, LIST_CONTAINERS);
  if (!container) {
    throw new PanoptoPageError(
      "page_structure_changed",
      "Panopto recording list was not found",
    );
  }
  const rows = all(container, LIST_ROWS);
  return rows.slice(0, 100).map((row) => {
    const link = first(row, TITLE_LINKS);
    if (!link) {
      throw new PanoptoPageError(
        "page_structure_changed",
        "Panopto recording link was not found",
      );
    }
    const parsed = viewer(link.getAttribute("href") || "");
    const timeNode = first(row, ["time", "[data-created-utc]"]);
    const rawTime = timeNode?.getAttribute("datetime")
      || timeNode?.getAttribute("data-created-utc")
      || "";
    const created = new Date(rawTime);
    if (Number.isNaN(created.getTime())) {
      throw new PanoptoPageError(
        "page_structure_changed",
        "Panopto recording time was not found",
      );
    }
    const name = (link.textContent || "").trim();
    if (!name) {
      throw new PanoptoPageError(
        "page_structure_changed",
        "Panopto recording title was not found",
      );
    }
    return {
      session_id: parsed.sessionId,
      name,
      created_utc: created.toISOString(),
      duration_seconds: parseDuration(text(row, ["[data-duration]", ".duration"])),
      folder_name: text(row, [".folder-name", "[data-folder-name]"]),
      viewer_url: parsed.url,
    };
  });
}

export async function waitForSharedRecordings(document, location, options = {}) {
  const maxAttempts = options.maxAttempts ?? 120;
  const settle = options.settle
    ?? (() => new Promise((resolve) => setTimeout(resolve, 250)));
  let navigationClicked = false;
  let lastError;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    if (isLoginRequired(document, location)) {
      throw new PanoptoPageError(
        "panopto_login_required",
        "Sign in to Panopto",
      );
    }
    try {
      return readSharedRecordings(document);
    } catch (error) {
      if (error.code !== "page_structure_changed") throw error;
      lastError = error;
    }
    if (!navigationClicked) {
      const control = [...document.querySelectorAll("a,button")].find(
        (item) => (item.textContent || "")
          .trim()
          .replace(/\s+/g, " ")
          .toLowerCase()
          .includes("shared with me"),
      );
      if (control) {
        control.click();
        navigationClicked = true;
      }
    }
    await settle();
  }
  throw lastError || new PanoptoPageError(
    "page_structure_changed",
    "Panopto recording list was not found",
  );
}

export async function readTranscript(document, options = {}) {
  const pane = first(document, TRANSCRIPT_PANES);
  if (!pane) {
    throw new PanoptoPageError(
      "page_structure_changed",
      "Panopto transcript panel was not found",
    );
  }
  const maxScrolls = options.maxScrolls ?? 200;
  const requiredStable = options.stablePasses ?? 3;
  const settle = options.settle
    ?? (() => new Promise((resolve) => setTimeout(resolve, 100)));
  let prior = "";
  let stable = 0;
  let latest = [];
  for (let pass = 0; pass < maxScrolls; pass += 1) {
    latest = all(pane, TRANSCRIPT_LINES)
      .map((line) => (line.textContent || "").trim())
      .filter(Boolean);
    const signature = latest.join("\n");
    stable = signature && signature === prior ? stable + 1 : 0;
    if (stable >= requiredStable) {
      return {
        complete: true,
        line_count: latest.length,
        text: signature,
      };
    }
    prior = signature;
    pane.scrollTop = Math.min(
      pane.scrollTop + Math.max(pane.clientHeight, 1),
      Math.max(pane.scrollHeight - pane.clientHeight, 0),
    );
    await settle();
  }
  if (!latest.length) {
    throw new PanoptoPageError(
      "transcript_processing",
      "Panopto transcript is still processing",
    );
  }
  throw new PanoptoPageError(
    "transcript_incomplete",
    "Panopto transcript did not load completely",
  );
}
