import type { RoleDashboardData } from '../api/client'

export const DASHBOARD_CACHE_FRESH_MS = 30_000
export const DASHBOARD_CACHE_MAX_AGE_MS = 5 * 60_000

const CACHE_PREFIX = 'binhu:role-dashboard:v1:'

export interface DashboardCacheEntry {
  data: RoleDashboardData
  ageMs: number
}

type DashboardStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem' | 'key' | 'length'>

function cacheKey(userId: number): string {
  return `${CACHE_PREFIX}${userId}`
}

export function readRoleDashboardCache(
  storage: DashboardStorage,
  userId: number,
  now = Date.now(),
): DashboardCacheEntry | null {
  try {
    const raw = storage.getItem(cacheKey(userId))
    if (!raw) return null
    const parsed = JSON.parse(raw) as { savedAt?: unknown; data?: RoleDashboardData }
    const savedAt = Number(parsed.savedAt)
    const data = parsed.data
    const ageMs = now - savedAt
    if (
      !data
      || Number(data.identity?.user_id) !== userId
      || !Number.isFinite(savedAt)
      || ageMs < 0
      || ageMs > DASHBOARD_CACHE_MAX_AGE_MS
    ) {
      storage.removeItem(cacheKey(userId))
      return null
    }
    return { data, ageMs }
  } catch {
    storage.removeItem(cacheKey(userId))
    return null
  }
}

export function writeRoleDashboardCache(
  storage: DashboardStorage,
  userId: number,
  data: RoleDashboardData,
  now = Date.now(),
): void {
  if (Number(data.identity?.user_id) !== userId) return
  try {
    storage.setItem(cacheKey(userId), JSON.stringify({ savedAt: now, data }))
  } catch {
    // Storage can be unavailable or full. The live dashboard remains usable.
  }
}

export function clearRoleDashboardCaches(storage: DashboardStorage): void {
  const keys: string[] = []
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index)
    if (key?.startsWith(CACHE_PREFIX)) keys.push(key)
  }
  keys.forEach(key => storage.removeItem(key))
}
