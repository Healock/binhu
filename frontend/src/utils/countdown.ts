export function formatCountdown(milliseconds: number): string {
  if (!Number.isFinite(milliseconds) || milliseconds <= 0) return '即将开始'
  const totalSeconds = Math.ceil(milliseconds / 1000)
  const days = Math.floor(totalSeconds / 86400)
  const hours = Math.floor((totalSeconds % 86400) / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60

  if (days > 0) return `${days}天${hours}小时`
  if (hours > 0) return `${hours}小时${minutes}分`
  return `${minutes.toString().padStart(2, '0')}:${seconds
    .toString()
    .padStart(2, '0')}`
}

export function getServerOffset(serverTime?: string | null): number {
  if (!serverTime) return 0
  const parsed = Date.parse(serverTime)
  return Number.isFinite(parsed) ? parsed - Date.now() : 0
}

export function getRemainingTime(
  nextRunAt?: string | null,
  serverOffset = 0,
  clientNow = Date.now(),
): number | null {
  if (!nextRunAt) return null
  const next = Date.parse(nextRunAt)
  if (!Number.isFinite(next)) return null
  return next - (clientNow + serverOffset)
}
