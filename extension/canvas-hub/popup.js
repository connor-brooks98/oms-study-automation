import {pair, pairingStatus} from "./lib/hub-client.js";

const status = document.querySelector("#status");
async function refresh() { status.textContent = await pairingStatus() ? "Paired" : "Not paired"; }
document.querySelector("#pair").addEventListener("click", async () => {
  try { await pair(document.querySelector("#code").value.trim()); status.textContent = "Paired"; }
  catch (error) { status.textContent = String(error); }
});
document.querySelector("#scan").addEventListener("click", async () => {
  status.textContent = "Scanning…";
  const result = await chrome.runtime.sendMessage({type: "scan-now"});
  status.textContent = result.status === "complete" ? `Found ${result.total} items` : result.status;
});
refresh();
