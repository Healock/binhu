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
  assert.doesNotMatch(pageSource, /online\.username/)
  assert.match(pageSource, /账号和统一密码不会从平台返回/)
  assert.match(apiSource, /account_mode: 'selected_community_account'/)
  assert.match(authSource, /currentUser\.role !== 'super_admin'/)
  assert.match(authSource, /cacheOnlineResidenceConfig\(config\)/)
  assert.match(authSource, /catch \{[\s\S]*cache is intentionally left untouched/)
})

test('离线配置使用完整账号且不会从社区代码拼接账号', () => {
  assert.match(pageSource, /完整登录账号/)
  assert.match(clientSource, /username: account\.username/)
  assert.match(clientSource, /community_\$\{account\.community_id\}/)
  assert.doesNotMatch(clientSource, /username: `\$\{communityCode\}00`/)
  assert.doesNotMatch(pageSource, /账号自动按/)
})

test('离线页拥有独立纵向滚动容器', () => {
  assert.match(pageSource, /offline-mode-page h-full min-h-0 overflow-y-auto overscroll-contain/)
})

test('在线范围变化移除范围外账号、保留本机密码且不保存会话令牌', () => {
  Object.defineProperty(globalThis, 'localStorage', { value: memoryStorage(), configurable: true })
  saveOfflineResidenceConfig({
    enabled: true,
    base_url: 'https://old.invalid',
    username: 'old-account',
    password: 'device-only-password',
    mac_service_url: 'http://127.0.0.1:23333',
    timeout_seconds: 15,
    community_codes: ['OLD'],
    login_community_ids: [12],
    login_community_names: ['旧社区'],
    accounts: [{ community_id: 12, community_name: '旧社区', username: 'old-account', community_code: 'OLD' }],
  })
  const cached = cacheOnlineResidenceConfig({
    enabled: false,
    base_url: 'https://new.invalid/',
    mac_service_url: 'http://127.0.0.1:24444/',
    timeout_seconds: 30,
    community_codes: ['NEW'],
    login_community_ids: [18],
    login_community_names: ['新社区'],
    login_community_codes: ['NEW'],
  })
  assert.equal(cached.password, 'device-only-password')
  assert.equal(cached.username, '')
  assert.equal(cached.base_url, 'https://new.invalid')
  assert.equal(cached.mac_service_url, 'http://127.0.0.1:24444')
  assert.deepEqual(cached.login_community_ids, [18])
  assert.deepEqual(cached.login_community_names, ['新社区'])
  assert.deepEqual(cached.accounts, [{ community_id: 18, community_name: '新社区', username: '', community_code: 'NEW' }])
  assert.equal(JSON.stringify(cached).includes('token'), false)
})

test('在线配置刷新保留仍在范围内的本机社区账号', () => {
  Object.defineProperty(globalThis, 'localStorage', { value: memoryStorage(), configurable: true })
  saveOfflineResidenceConfig({
    ...loadOfflineResidenceConfig(),
    enabled: true,
    base_url: 'https://old.invalid',
    password: 'device-only-password',
    mac_service_url: 'http://127.0.0.1:23333',
    login_community_ids: [12],
    login_community_names: ['测试社区'],
    accounts: [{ community_id: 12, community_name: '测试社区', username: 'device-account', community_code: 'OLD' }],
  })
  const cached = cacheOnlineResidenceConfig({
    enabled: true,
    base_url: 'https://new.invalid',
    mac_service_url: 'http://127.0.0.1:24444',
    login_community_ids: [12, 18],
    login_community_names: ['测试社区', '第二社区'],
    login_community_codes: ['A123456789', 'B123456789'],
  })
  assert.deepEqual(cached.accounts, [
    { community_id: 12, community_name: '测试社区', username: 'device-account', community_code: 'A123456789' },
    { community_id: 18, community_name: '第二社区', username: '', community_code: 'B123456789' },
  ])
})

test('在线多社区范围为每个社区建立独立的本地账号槽位', () => {
  Object.defineProperty(globalThis, 'localStorage', { value: memoryStorage(), configurable: true })
  saveOfflineResidenceConfig({
    ...loadOfflineResidenceConfig(),
    enabled: true,
    base_url: 'https://old.invalid',
    username: 'device-account',
    password: 'device-only-password',
    mac_service_url: 'http://127.0.0.1:23333',
    community_codes: ['OLD'],
  })
  const cached = cacheOnlineResidenceConfig({
    enabled: true,
    base_url: 'https://new.invalid',
    mac_service_url: 'http://127.0.0.1:24444',
    login_community_ids: [12, 18],
    login_community_names: ['测试社区', '第二社区'],
    login_community_codes: ['A123456789', 'B123456789'],
  })
  assert.deepEqual(cached.login_community_ids, [12, 18])
  assert.deepEqual(cached.login_community_names, ['测试社区', '第二社区'])
  assert.deepEqual(cached.accounts, [
    { community_id: 12, community_name: '测试社区', username: '', community_code: 'A123456789' },
    { community_id: 18, community_name: '第二社区', username: '', community_code: 'B123456789' },
  ])
  assert.equal(cached.password, 'device-only-password')
})

test('离线页面按社区维护账号且不从远端回传凭据', () => {
  assert.match(pageSource, /config\.accounts\.map/)
  assert.match(pageSource, /添加本地社区账号/)
  assert.match(pageSource, /每个选中社区必须在当前客户端填写自己的完整登录账号/)
  assert.match(pageSource, /账号不会根据组织代码自动拼接/)
  assert.doesNotMatch(pageSource, /online\.username/)
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
    mac_service_url: '',
  })
  assert.deepEqual(preserved, migrated)
})
