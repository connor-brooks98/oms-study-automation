import {CanvasLoginRequiredError} from "./canvas-api.js";
import {discoverCourse} from "./discovery.js";
import {downloadDisposition} from "./downloads.js";
import * as hub from "./hub-client.js";

let activeScan = null;

async function scan(dependencies) {
  const client = dependencies.hub || hub;
  const discover = dependencies.discoverCourse || discoverCourse;
  const download = dependencies.downloadDisposition || downloadDisposition;
  await client.heartbeat("scanning");
  try {
    const config = await client.getConfig();
    let total = 0;
    let newCount = 0;
    for (const course of config.courses.filter((item) => item.enabled)) {
      const items = await discover(course);
      total += items.length;
      for (let offset = 0; offset < items.length; offset += 100) {
        const batch = items.slice(offset, offset + 100);
        const result = await client.postDiscover(batch);
        for (let index = 0; index < result.dispositions.length; index += 1) {
          const disposition = result.dispositions[index];
          if (disposition.action === "download") {
            await download(disposition, batch[index]);
            newCount += 1;
          }
        }
      }
    }
    await client.heartbeat("connected", {scan_complete: true, item_count: total, new_count: newCount});
    return {status: "complete", total, newCount};
  } catch (error) {
    if (error instanceof CanvasLoginRequiredError) {
      await client.heartbeat("canvas_login_required", {error: "Sign back into LMU Canvas"});
      return {status: "canvas_login_required"};
    }
    await client.heartbeat("error", {error: String(error).slice(0, 500)}).catch(() => {});
    throw error;
  }
}

export function runScan(dependencies = {}) {
  if (activeScan) return activeScan;
  activeScan = scan(dependencies).finally(() => { activeScan = null; });
  return activeScan;
}
