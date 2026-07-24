export function createCommandPoller({
  getConfig,
  runScan,
  getPanoptoCommand,
  runPanoptoCommand,
  panoptoHub,
}) {
  let activePanopto = null;
  return async function pollCommands() {
    const config = await getConfig();
    if (config.scan_requested) runScan().catch(() => {});
    if (activePanopto) return activePanopto;
    const command = await getPanoptoCommand();
    if (!command) return null;
    activePanopto = runPanoptoCommand(command, {hub: panoptoHub})
      .finally(() => {
        activePanopto = null;
      });
    return activePanopto;
  };
}
