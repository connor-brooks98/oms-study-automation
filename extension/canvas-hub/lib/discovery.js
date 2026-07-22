import {canvasFetch, canvasFetchJson, listAll} from "./canvas-api.js";

const ORIGIN = "https://lmunet.instructure.com";

function plainText(html) {
  return html.replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 500);
}

export function extractFileIds(pageHtml) {
  const ids = new Set();
  for (const match of pageHtml.matchAll(/href=["']([^"']+)["']/gi)) {
    const url = new URL(match[1], ORIGIN);
    const file = url.pathname.match(/^\/courses\/\d+\/files\/(\d+)(?:\/download)?$|^\/files\/(\d+)\/download$/);
    if (url.origin === ORIGIN && file) ids.add(file[1] || file[2]);
  }
  return [...ids];
}

function metadata(course, module, item, page, file, evidenceText = "") {
  return {
    course_id: String(course.course_id), course_name: course.course_name,
    course_code: course.course_code || "", module_id: String(module.id),
    module_title: module.name || "", item_id: String(item.id), item_title: item.title || "",
    item_type: item.type, page_url: page?.url || "", page_title: page?.title || item.title || "",
    file_id: String(file.id), filename: file.filename || file.display_name,
    content_type: file["content-type"] || file.content_type || "application/octet-stream",
    size: file.size || 0, modified_at: file.modified_at || file.updated_at || "",
    download_url: file.url, evidence_text: evidenceText,
  };
}

export async function discoverCourse(course, fetchImpl = fetch) {
  const modules = await listAll(`/api/v1/courses/${course.course_id}/modules?include[]=items&per_page=100`, fetchImpl);
  const found = new Map();
  for (const module of modules) {
    for (const item of module.items || []) {
      if (item.type === "File") {
        const {data: file} = await canvasFetchJson(`/api/v1/files/${item.content_id}`, fetchImpl);
        found.set(String(file.id), metadata(course, module, item, null, file));
      } else if (item.type === "Page") {
        const {data: page} = await canvasFetchJson(`/api/v1/courses/${course.course_id}/pages/${encodeURIComponent(item.page_url)}`, fetchImpl);
        const evidence = plainText(page.body || "");
        for (const fileId of extractFileIds(page.body || "")) {
          const {data: file} = await canvasFetchJson(`/api/v1/files/${fileId}`, fetchImpl);
          found.set(String(file.id), metadata(course, module, item, page, file, evidence));
        }
      }
    }
  }
  return [...found.values()];
}

export async function fetchCanvasPage(path, fetchImpl = fetch) {
  return (await canvasFetch(path, fetchImpl)).text;
}
