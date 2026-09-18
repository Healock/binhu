import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  cacheOnlineResidenceConfig,
  loadOfflineResidenceConfig,
  saveOfflineResidenceConfig,
} from '../src/utils/offlineResidenceClient.ts'

const pageSource = readFileSync(new URL('../src/pages/OfflineMode.tsx', import.meta.url), 'utf8')
const clientSource = readFileSync(new URL('../src/utils/offlineResidenceClient.ts', import.meta.url), 'utf8')
const workbookSource = readFileSync(new URL('../src/utils/offlineResidenceXlsx.ts', import.meta.url), 'utf8')
const apiSource = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8')
const authSource = readFileSync(new URL('../src/context/AuthContext.tsx', import.meta.url), 'utf8')

function memoryStorage() {
  const values = new Map<string, string>()
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, String(value)) },
    removeItem: (key: string) => { values.delete(key) },
    clear: () => values.clear(),
    key: (index: number) => Array.from(values.keys())[index] ?? null,
    get length() { return values.size },
  }
}

test('离线批量查询不依赖滨湖平台批量接口', () => {
  assert.match(pageSource, /readOfflineWorkbook\(file\)/)
  assert.match(pageSource, /new OfflineResidenceClient\(config\)/)
  assert.match(pageSource, /writeOfflineWorkbook\(workbook, statuses\)/)
  assert.doesNotMatch(pageSource, /startResidenceBatchQuery|getResidenceBatchQuery|exportResidenceBatchQuery/)
  assert.doesNotMatch(apiSource, /startResidenceBatchQuery|getResidenceBatchQuery|exportResidenceBatchQuery/)
})

test('离线页面展示整体进度和查询成功人数', () => {
  assert.match(pageSource, /<Progress[\s\S]*completed \/ total/)
  assert.match(pageSource, /总人数 \{total\}，查询成功 \{successCount\}/)
  assert.match(pageSource, /setCompleted\(completedCount\)/)
})

test('工作簿只查找身份证列并在其后插入登记情况', () => {
  assert.match(workbookSource, /身份证号.*身份证号码.*身份证/)
  assert.match(workbookSource, /header\.splice\(book\.identityColumn \+ 1, 0, '登记情况'\)/)
  assert.match(workbookSource, /不校验|不检查|identityColumn/)
})

test('离线客户端只调用居住证只读登录和查询路径', () => {
  for (const path of ['/sys/randomImage/', '/sys/login', '/szjzz/searchIsck', '/szjzz/searchzzrk']) {
    assert.match(clientSource, new RegExp(path.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
  }
  assert.doesNotMatch(clientSource, /\/registration\/submit|\/delete|\/writeback/i)
})

test('在线配置同步不要求密码明文', () => {
  assert.match(pageSource, /getResidencePlatformConfig\(\)/)
  assert.match(pageSource, /online\.username/)
  assert.match(pageSource, /统一登录密码不会回传/)
  assert.match(apiSource, /account_mode: 'configured_full_username'/)
  assert.match(authSource, /currentUser\.role !== 'super_admin'/)
  assert.match(authSource, /cacheOnlineResidenceConfig\(config\)/)
  assert.match(authSource, /catch \{[\s\S]*cache is intentionally left untouched/)
})

test('离线配置使用完整账号且不会从社区代码拼接账号', () => {
  assert.match(pageSource, /完整登录账号/)
  assert.match(clientSource, /username: this\.config\.username/)
  assert.doesNotMatch(clientSource, /username: `\$\{communityCode\}00`/)
  assert.doesNotMatch(pageSource, /账号自动按/)
})

test('离线页拥有独立纵向滚动容器', () => {
  assert.match(pageSource, /offline-mode-page h-full min-h-0 overflow-y-auto overscroll-contain/)
})

test('在线配置缓存保留本机密码且不保存会话令牌', () => {
  Object.defineProperty(globalThis, 'localStorage', { value: memoryStorage(), configurable: true })
  saveOfflineResidenceConfig({
    enabled: true,
    base_url: 'https://old.invalid',
    username: 'old-account',
    password: 'device-only-password',
    mac_service_url: 'http://127.0.0.1:23333',
    timeout_seconds: 15,
    community_codes: ['OLD'],
  })
  const cached = cacheOnlineResidenceConfig({
    enabled: false,
    base_url: 'https://new.invalid/',
    username: 'full-account',
    mac_service_url: 'http://127.0.0.1:24444/',
    timeout_seconds: 30,
    community_codes: ['NEW'],
  })
  assert.equal(cached.password, 'device-only-password')
  assert.equal(cached.username, 'full-account')
  assert.equal(cached.base_url, 'https://new.invalid')
  assert.equal(cached.mac_service_url, 'http://127.0.0.1:24444')
  assert.equal(JSON.stringify(cached).includes('token'), false)
})

test('不完整在线响应和旧版社区代码缓存不会猜测或覆盖账号', () => {
  Object.defineProperty(globalThis, 'localStorage', { value: memoryStorage(), configurable: true })
  localStorage.setItem('binhu_offline_residence_config_v1', JSON.stringify({
    enabled: true,
    base_url: 'https://old.invalid',
    password: 'device-only-password',
    mac_service_url: 'http://127.0.0.1:23333',
    timeout_seconds: 15,
    community_codes: ['A123456789'],
  }))
  const migrated = loadOfflineResidenceConfig()
  assert.equal(migrated.username, '')
  const preserved = cacheOnlineResidenceConfig({
    base_url: '',
    username: '',
    mac_service_url: '',
  })
  assert.deepEqual(preserved, migrated)
})
