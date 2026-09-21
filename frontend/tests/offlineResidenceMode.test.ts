import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  OfflineResidenceClient,
  cacheOnlineResidenceConfig,
  loadOfflineResidenceConfig,
  normalizeMacAddress,
  readMacAddress,
  saveOfflineResidenceConfig,
  summarizeResidencePayload,
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

test('离线诊断摘要只保留阶段和响应元数据，不包含响应正文', () => {
  const summary = summarizeResidencePayload({ success: false, code: 500, message: '姓名和身份证等业务正文不应出现在诊断中', result: null }, 200)
  assert.deepEqual(summary, {
    http_status: 200,
    business_code: '500',
    success: false,
    result_type: 'null',
    message_category: 'other',
  })
  for (const key of ['message', 'result', 'token', 'sfzh']) assert.equal(Object.prototype.hasOwnProperty.call(summary, key), false)
})

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

test('MAC 地址只接受规范化的十六进制地址', () => {
  assert.equal(normalizeMacAddress('aa-bb-cc-dd-ee-ff'), 'AA:BB:CC:DD:EE:FF')
  assert.equal(normalizeMacAddress('aabb.ccdd.eeff'), 'AA:BB:CC:DD:EE:FF')
  assert.throws(() => normalizeMacAddress('not-a-mac'), /12 位十六进制/)
  assert.throws(() => normalizeMacAddress('00:00:00:00:00:00'), /格式无效/)
  assert.throws(() => normalizeMacAddress('01:00:00:00:00:00'), /格式无效/)
})

test('MAC 兼容读取只访问固定本机 23333 端口', async () => {
  Object.defineProperty(globalThis, 'window', { value: globalThis, configurable: true })
  const requests: Array<{ url: string; init?: RequestInit }> = []
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async (url: string, init?: RequestInit) => {
      requests.push({ url, init })
      return new Response(JSON.stringify({ mac: 'AA:BB:CC:DD:EE:FE' }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    },
  })
  const config = { ...loadOfflineResidenceConfig(), mac_service_url: 'http://127.0.0.1:23333', timeout_seconds: 2 }
  assert.equal(await readMacAddress(config), 'AA:BB:CC:DD:EE:FE')
  assert.equal(requests[0].url, 'http://127.0.0.1:23333')
  assert.equal(requests[0].init?.method, 'GET')
})

test('离线页通过客户端 bridge 读取、保存并回读本机 MAC', () => {
  assert.match(pageSource, /读取当前 MAC/)
  assert.match(pageSource, /保存本机 MAC/)
  assert.match(pageSource, /resolveDesktopBridge/)
  assert.match(pageSource, /desktop\.getLocalMac\(\)/)
  assert.match(pageSource, /desktop\.setLocalMac\(mac\)/)
  assert.match(pageSource, /本机 MAC 保存后回读不一致/)
  assert.doesNotMatch(pageSource, /MAC 写入令牌|保存并推送 MAC/)
  assert.doesNotMatch(clientSource, /X-Macmock-Token|pushMacAddress/)
})

test('候选 MAC 按社区账号顺序检测授权范围且不调用业务写接口', async () => {
  Object.defineProperty(globalThis, 'window', { value: globalThis, configurable: true })
  const loginBodies: Array<Record<string, unknown>> = []
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async (url: string, init?: RequestInit) => {
      if (url.includes('/sys/randomImage/')) return new Response(JSON.stringify({ success: true }), { status: 200 })
      assert.match(url, /\/sys\/login$/)
      assert.equal(init?.method, 'POST')
      const body = JSON.parse(String(init?.body || '{}'))
      loginBodies.push(body)
      return new Response(JSON.stringify(body.username === 'community-a'
        ? { success: true, result: { token: 'fixture-token', orgCode: 'A123456789' } }
        : { success: false, message: 'fixture-rejected' }), { status: 200 })
    },
  })
  const config = {
    ...loadOfflineResidenceConfig(),
    base_url: 'https://residence.invalid',
    password: 'fixture-password',
    accounts: [
      { community_id: 1, community_name: '第一社区', username: 'community-a', community_code: 'A123456789' },
      { community_id: 2, community_name: '第二社区', username: 'community-b', community_code: 'B123456789' },
    ],
  }
  const results = await new OfflineResidenceClient(config).probeMacAccess('02-11-22-33-44-66')
  assert.deepEqual(results.map(result => [result.community_name, result.allowed]), [
    ['第一社区', true],
    ['第二社区', false],
  ])
  assert.deepEqual(loginBodies.map(body => body.mac), ['02:11:22:33:44:66', '02:11:22:33:44:66'])
  assert.doesNotMatch(clientSource, /\/(?:registration|writeback|delete)/i)
})

test('居住证接口地址保留可选应用上下文路径', async () => {
  Object.defineProperty(globalThis, 'window', { value: globalThis, configurable: true })
  const urls: string[] = []
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async (url: string) => {
      urls.push(url)
      return new Response(JSON.stringify(url.includes('/sys/randomImage/')
        ? { success: true }
        : { success: true, result: { token: 'fixture-token', orgCode: '' } }), { status: 200 })
    },
  })
  const result = await new OfflineResidenceClient({
    ...loadOfflineResidenceConfig(),
    base_url: 'https://residence.invalid/grandlynn-boot',
    password: 'fixture-password',
    accounts: [{ community_id: 1, community_name: '测试社区', username: 'fixture-user', community_code: '' }],
  }).probeMacAccess('02:11:22:33:44:66')
  assert.equal(result[0].status, 'allowed')
  assert.ok(urls[0].startsWith('https://residence.invalid/grandlynn-boot/sys/randomImage/'))
  assert.equal(urls[1], 'https://residence.invalid/grandlynn-boot/sys/login')
})

test('验证码返回非 JSON 时保留可诊断错误码', async () => {
  Object.defineProperty(globalThis, 'window', { value: globalThis, configurable: true })
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => new Response('<html>not json</html>', { status: 200, headers: { 'Content-Type': 'text/html' } }),
  })
  const result = await new OfflineResidenceClient({
    ...loadOfflineResidenceConfig(),
    base_url: 'https://residence.invalid/grandlynn-boot',
    password: 'fixture-password',
    accounts: [{ community_id: 1, community_name: '测试社区', username: 'fixture-user', community_code: '' }],
  }).probeMacAccess('02:11:22:33:44:66')
  assert.equal(result[0].status, 'network_error')
  assert.equal(result[0].error_code, 'invalid_response')
})

test('候选 MAC 探测最多使用前 12 个已填写完整账号的社区', async () => {
  Object.defineProperty(globalThis, 'window', { value: globalThis, configurable: true })
  const usernames: string[] = []
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async (url: string, init?: RequestInit) => {
      if (url.includes('/sys/randomImage/')) return new Response(JSON.stringify({ success: true }), { status: 200 })
      const body = JSON.parse(String(init?.body || '{}'))
      usernames.push(String(body.username || ''))
      return new Response(JSON.stringify({ success: true, result: { token: 'fixture-token', orgCode: '' } }), { status: 200 })
    },
  })
  const accounts = Array.from({ length: 13 }, (_, index) => ({
    community_id: index + 1,
    community_name: `测试社区${index + 1}`,
    username: `community-${index + 1}`,
    community_code: '',
  }))
  const results = await new OfflineResidenceClient({
    ...loadOfflineResidenceConfig(),
    base_url: 'https://residence.invalid',
    password: 'fixture-password',
    accounts,
  }).probeMacAccess('02:11:22:33:44:66')

  assert.equal(results.length, 12)
  assert.deepEqual(usernames, accounts.slice(0, 12).map(account => account.username))
})

test('MAC 探测不会把网络失败伪装成未授权', async () => {
  Object.defineProperty(globalThis, 'window', { value: globalThis, configurable: true })
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => { throw new TypeError('Failed to fetch') },
  })
  const result = await new OfflineResidenceClient({
    ...loadOfflineResidenceConfig(),
    base_url: 'https://residence.invalid',
    password: 'fixture-password',
    accounts: [{ community_id: 1, community_name: '测试社区', username: 'fixture-user', community_code: '' }],
  }).probeMacAccess('02:11:22:33:44:66')
  assert.equal(result[0].allowed, false)
  assert.equal(result[0].status, 'network_error')
  assert.equal(result[0].error_code, 'request_failed')
  assert.match(clientSource, /status: OfflineMacProbeResult\['status'\]/)
})

test('登录成功后允许社区代码与返回组织代码的层级后缀兼容', async () => {
  Object.defineProperty(globalThis, 'window', { value: globalThis, configurable: true })
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async (url: string) => new Response(JSON.stringify(url.includes('/sys/randomImage/')
      ? { success: true }
      : { success: true, result: { token: 'fixture-token', userInfo: { orgCode: '320584037700' } } }), { status: 200 }),
  })
  const result = await new OfflineResidenceClient({
    ...loadOfflineResidenceConfig(),
    base_url: 'https://residence.invalid',
    password: 'fixture-password',
    accounts: [{ community_id: 1, community_name: '测试社区', username: 'fixture-user', community_code: '3205840377' }],
  }).probeMacAccess('02:11:22:33:44:66')
  assert.equal(result[0].allowed, true)
  assert.equal(result[0].status, 'allowed')
})

test('在线配置同步不要求密码明文', () => {
  assert.match(pageSource, /getResidencePlatformConfig\(\)/)
  assert.doesNotMatch(pageSource, /online\.username/)
  assert.match(pageSource, /社区完整登录账号/)
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
    ...loadOfflineResidenceConfig(),
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
  assert.equal(cached.mac_service_url, 'http://127.0.0.1:23333')
  assert.deepEqual(cached.login_community_ids, [18])
  assert.deepEqual(cached.login_community_names, ['新社区'])
  assert.deepEqual(cached.accounts, [{ community_id: 18, community_name: '新社区', username: '', community_code: 'NEW' }])
  assert.equal(JSON.stringify(cached).includes('access_token'), false)
})

test('在线配置同步 selected community 的完整账号到本机', () => {
  Object.defineProperty(globalThis, 'localStorage', { value: memoryStorage(), configurable: true })
  const cached = cacheOnlineResidenceConfig({
    enabled: true,
    base_url: 'https://new.invalid/grandlynn-boot',
    login_community_ids: [12],
    login_community_names: ['测试社区'],
    login_community_codes: ['A123456789'],
    login_community_usernames: ['community-login'],
  })
  assert.equal(cached.accounts[0].username, 'community-login')
  assert.equal(cached.accounts[0].community_code, 'A123456789')
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
  assert.match(pageSource, /每个选中社区使用社区管理中配置的完整登录账号/)
  assert.match(pageSource, /账号不会根据组织代码自动拼接/)
  assert.doesNotMatch(pageSource, /online\.username/)
})

test('离线页面提供脱敏诊断导出并显示批量失败分类', () => {
  assert.match(pageSource, /导出诊断信息/)
  assert.match(pageSource, /error_counts/)
  assert.match(pageSource, /native_residence_bridge_available/)
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
