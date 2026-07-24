export function createCommandPoller({
  getConfig,
  runScan,
  getPanoptoRequest,
  runPanoptoRequest,
  panoptoHub,
}) {
  let activePanopto = null;
  let pollInFlight = null;
  return function pollCommands() {
    if (pollInFlight) return pollInFlight;
    pollInFlight = (async () => {
      const config = await getConfig();
      if (config.scan_requested) runScan().catch(() => {});
      if (activePanopto) return activePanopto;
      const request = await getPanoptoRequest();
      if (!request) return null;
      activePanopto = runPanoptoRequest(request, {hub: panoptoHub})
        .finally(() => {
          activePanopto = null;
        });
      return activePanopto;
    })().finally(() => {
      pollInFlight = null;
    });
    return pollInFlight;
  };
}
