export const EXTERNAL_RUN_POLL_INTERVAL_MS = 5000

export function hasActiveExternalAcquisitionRun(
  runs: readonly { status: string }[],
): boolean {
  return runs.some(run => run.status === 'queued' || run.status === 'running')
}
