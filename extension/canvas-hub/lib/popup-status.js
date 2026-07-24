export function formatPanoptoResult(result) {
  if (!result) return "No Panopto command pending";
  if (result.error) {
    return `Error: ${String(result.error).slice(0, 300)}`;
  }
  if (result.reason_code) {
    const state = String(result.status || "failed");
    return `${state.charAt(0).toUpperCase()}${state.slice(1)}: ${result.reason_code}`;
  }
  return result.status || "No Panopto command pending";
}
