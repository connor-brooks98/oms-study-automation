const LIST_CONTAINERS = ["#listViewContainer", "[data-testid='session-list']"];
const LIST_ROWS = ["tbody tr.list-view-row", "[data-session-id]"];
const TITLE_LINKS = [
  ".item-title.title-link a.detail-title",
  "a[href*='/Panopto/Pages/Viewer.aspx?id=']",
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

export function newestSharedRecording(recordings) {
  if (!Array.isArray(recordings) || !recordings.length) {
    throw new PanoptoPageError(
      "no_shared_recordings",
      "No shared Panopto recordings were found",
    );
  }
  return recordings.reduce((newest, current) => {
    const currentTime = Date.parse(current.created_utc);
    const newestTime = Date.parse(newest.created_utc);
    if (!Number.isFinite(currentTime) || !Number.isFinite(newestTime)) {
      throw new PanoptoPageError(
        "page_structure_changed",
        "Panopto recording time is invalid",
      );
    }
    return currentTime > newestTime ? current : newest;
  });
}

function normalized(value) {
  return (value || "").trim().replace(/\s+/g, " ").toLowerCase();
}

function isEnglishUsa(control) {
  const metadata = [
    control.getAttribute("data-language"),
    control.getAttribute("data-language-code"),
    control.getAttribute("lang"),
    control.getAttribute("aria-label"),
    control.getAttribute("title"),
    control.textContent,
  ].map(normalized).join(" ");
  return (
    metadata.includes("english_usa")
    || metadata.includes("english (united states)")
    || metadata.includes("en-us")
  );
}

function isCaptionControl(control) {
  const label = normalized([
    control.textContent,
    control.getAttribute("aria-label"),
    control.getAttribute("title"),
  ].filter(Boolean).join(" "));
  return label.includes("download caption");
}

function captionUrl(control, location) {
  const raw = control.getAttribute("href")
    || control.getAttribute("data-download-url")
    || control.getAttribute("data-caption-download-url");
  if (!raw) return null;
  let url;
  try {
    url = new URL(raw, location.origin);
  } catch {
    throw new PanoptoPageError(
      "unsafe_caption_download",
      "Caption download URL is invalid",
    );
  }
  if (
    url.protocol !== "https:"
    || url.hostname !== "lmunet.hosted.panopto.com"
  ) {
    throw new PanoptoPageError(
      "unsafe_caption_download",
      "Caption download is outside LMU Panopto",
    );
  }
  return url.toString();
}

export function readCaptionDownload(document, location) {
  const controls = [...document.querySelectorAll("a,button")];
  for (const control of controls) {
    if (!isCaptionControl(control) || !isEnglishUsa(control)) continue;
    const downloadUrl = captionUrl(control, location);
    if (!downloadUrl) continue;
    const requestedName = control.getAttribute("download") || "";
    const filename = requestedName.toLowerCase().endsWith(".txt")
      ? requestedName
      : "captions.txt";
    return {
      status: "ready",
      language: "English_USA",
      download_url: downloadUrl,
      filename,
    };
  }
  return {status: "captions_pending"};
}

export async function waitForCaptionDownload(document, location, options = {}) {
  const maxAttempts = options.maxAttempts ?? 8;
  const settle = options.settle
    ?? (() => new Promise((resolve) => setTimeout(resolve, 100)));
  let revealed = false;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const result = readCaptionDownload(document, location);
    if (result.status === "ready") return result;
    if (!revealed) {
      const control = [...document.querySelectorAll("a,button")].find(
        (item) => isCaptionControl(item) && !captionUrl(item, location),
      );
      if (control) {
        control.click();
        revealed = true;
      }
    }
    await settle();
  }
  return {status: "captions_pending"};
}
