import {pair, pairingStatus} from "./lib/hub-client.js";
import {postCourses} from "./lib/hub-client.js";
import {listAll} from "./lib/canvas-api.js";
import {formatPanoptoResult} from "./lib/popup-status.js";

const status = document.querySelector("#status");
async function refresh() { status.textContent = await pairingStatus() ? "Paired" : "Not paired"; }
document.querySelector("#pair").addEventListener("click", async () => {
  try {
    await pair(document.querySelector("#code").value.trim());
    const courses = await listAll("/api/v1/courses?enrollment_state=active&per_page=100");
    await postCourses(courses.map((item) => ({course_id: String(item.id), course_name: item.name || "", course_code: item.course_code || ""})));
    status.textContent = "Paired — courses sent to the Hub";
  }
  catch (error) { status.textContent = String(error); }
});
document.querySelector("#scan").addEventListener("click", async () => {
  status.textContent = "Scanning…";
  const result = await chrome.runtime.sendMessage({type: "scan-now"});
  status.textContent = result.status === "complete" ? `Found ${result.total} items` : result.status;
});
document.querySelector("#panopto").addEventListener("click", async () => {
  status.textContent = "Checking Panopto…";
  const result = await chrome.runtime.sendMessage({type: "panopto-scan-now"});
  status.textContent = formatPanoptoResult(result);
});
refresh();
