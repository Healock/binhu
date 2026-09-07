export const AUDIT_STREAM_SEEN_LIMIT = 5000

export function rememberAuditId(seen: Set<number>, id: number): boolean {
  if (!Number.isSafeInteger(id) || id < 0 || seen.has(id)) return false
  seen.add(id)
  while (seen.size > AUDIT_STREAM_SEEN_LIMIT) {
    const oldest = seen.values().next().value as number | undefined
    if (oldest === undefined) break
    seen.delete(oldest)
  }
  return true
}

export function auditStreamUrl(cursor: number, action: string, baseUrl: string): string {
  const query = new URLSearchParams({ after_id: String(Math.max(0, cursor)) })
  if (action) query.set('action', action)
  return `${baseUrl}?${query.toString()}`
}

export function clearAuditStreamTimer(
  timer: number | null,
  clearTimer: (handle: number) => void,
): null {
  if (timer !== null) clearTimer(timer)
  return null
}
