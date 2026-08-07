import assert from 'node:assert/strict'
import test from 'node:test'
import {
  clearRoleDashboardCaches,
  DASHBOARD_CACHE_FRESH_MS,
  DASHBOARD_CACHE_MAX_AGE_MS,
  readRoleDashboardCache,
  writeRoleDashboardCache,
} from '../src/utils/dashboardCache.ts'
import type { RoleDashboardData } from '../src/api/client.ts'

class MemoryStorage {
  private readonly values = new Map<string, string>()

  get length() { return this.values.size }
  getItem(key: string) { return this.values.get(key) ?? null }
  setItem(key: string, value: string) { this.values.set(key, value) }
  removeItem(key: string) { this.values.delete(key) }
  key(index: number) { return [...this.values.keys()][index] ?? null }
}

function dashboard(userId: number): RoleDashboardData {
  return {
    identity: { user_id: userId },
  } as RoleDashboardData
}

test('仪表盘缓存按账号隔离并在五分钟后失效', () => {
  const storage = new MemoryStorage()
  const now = 1_000_000
  writeRoleDashboardCache(storage, 7, dashboard(7), now)
  assert.equal(
    readRoleDashboardCache(storage, 7, now + DASHBOARD_CACHE_FRESH_MS)?.ageMs,
    DASHBOARD_CACHE_FRESH_MS,
  )
  assert.equal(readRoleDashboardCache(storage, 8, now + 1), null)
  assert.equal(
    readRoleDashboardCache(storage, 7, now + DASHBOARD_CACHE_MAX_AGE_MS + 1),
    null,
  )
})

test('仪表盘缓存拒绝身份不匹配数据并可在退出时统一清除', () => {
  const storage = new MemoryStorage()
  writeRoleDashboardCache(storage, 7, dashboard(8), 1_000)
  assert.equal(readRoleDashboardCache(storage, 7, 1_001), null)

  writeRoleDashboardCache(storage, 7, dashboard(7), 2_000)
  storage.setItem('unrelated', 'keep')
  clearRoleDashboardCaches(storage)
  assert.equal(readRoleDashboardCache(storage, 7, 2_001), null)
  assert.equal(storage.getItem('unrelated'), 'keep')
})
