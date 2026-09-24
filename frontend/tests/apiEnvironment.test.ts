import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  assertApiEnvironmentIdentity,
  assertLoginEnvironmentEntry,
  environmentForUsername,
  getApiBaseUrl,
  getApiEnvironment,
  resetApiEnvironment,
  resolveRuntimeAssetUrl,
  resolveRuntimeApiUrl,
  setApiEnvironment,
} from '../src/utils/apiEnvironment.ts'
import {
  exportGridMembersUrl,
  fetchWithAuth,
  fullchainArchiveDownloadUrl,
  fullchainPoliceRawDownloadUrl,
  policeDispatchFeedbackUrl,
  policeDispatchSourceFileUrl,
  resetUnauthorizedRedirectForTests,
  workflowApi,
} from '../src/api/client.ts'

function installSessionStorage() {
  const values = new Map<string, string>()
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
    },
  })
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { location: { pathname: '/login', href: '/login', origin: 'https://example.test' } },
  })
  return values
}

test('account suffix selects an environment without fuzzy matching', () => {
  assert.equal(environmentForUsername('observer@shadow'), 'shadow')
  assert.equal(environmentForUsername(' Observer@Shadow '), 'shadow')
  assert.equal(environmentForUsername('observer@staging'), 'staging')
  assert.equal(environmentForUsername('observer@dev'), 'development')
  assert.equal(environmentForUsername('shadow-observer'), 'production')
  assert.equal(environmentForUsername('observer@staging.example'), 'production')
})

test('login page has no manual staging environment entry', () => {
  const loginSource = readFileSync(new URL('../src/pages/Login.tsx', import.meta.url), 'utf8')
  assert.doesNotMatch(loginSource, /staging-environment-button/)
  assert.doesNotMatch(loginSource, /进入预发布环境/)
  assert.doesNotMatch(loginSource, /返回正式环境/)
  assert.doesNotMatch(loginSource, /window\.location\.(?:href|assign)\(?.*username/)
})

test('staging login depends on the account suffix on web and desktop while isolated paths reject mismatches', () => {
  assert.doesNotThrow(() => assertLoginEnvironmentEntry('staging', ''))
  assert.doesNotThrow(() => assertLoginEnvironmentEntry('staging', '/staging'))
  assert.doesNotThrow(() => assertLoginEnvironmentEntry('development', '/dev'))
  assert.doesNotThrow(() => assertLoginEnvironmentEntry('production', ''))

  assert.throws(
    () => assertLoginEnvironmentEntry('development', ''),
    /请先打开 \/dev\/ 入口/,
  )
  assert.throws(
    () => assertLoginEnvironmentEntry('production', '/staging'),
    /账号不属于当前环境/,
  )
  assert.throws(
    () => assertLoginEnvironmentEntry('staging', '/dev'),
    /账号不属于当前环境/,
  )
})

test('root web login routes a strict staging suffix to the fixed staging API', () => {
  installSessionStorage()
  assert.equal(environmentForUsername('observer@staging'), 'staging')
  assert.doesNotThrow(() => assertLoginEnvironmentEntry('staging', ''))

  setApiEnvironment(environmentForUsername('observer@staging'))
  assert.equal(getApiEnvironment(), 'staging')
  assert.equal(getApiBaseUrl(), '/staging/api')
  assert.equal(resolveRuntimeApiUrl('/api/app/bootstrap'), '/staging/api/app/bootstrap')
  assert.equal(resolveRuntimeApiUrl('/api/auth/login'), '/staging/api/auth/login')
})

test('shadow environment stays in session storage and resolves only the fixed path', () => {
  installSessionStorage()
  setApiEnvironment('shadow')
  assert.equal(getApiEnvironment(), 'shadow')
  assert.equal(getApiBaseUrl(), '/shadow-api')
  assert.equal(resolveRuntimeApiUrl('/api/auth/login'), '/shadow-api/auth/login')
  resetApiEnvironment()
  assert.equal(getApiEnvironment(), 'production')

  setApiEnvironment('staging')
  assert.equal(getApiEnvironment(), 'staging')
  assert.equal(getApiBaseUrl(), '/staging/api')
  assert.equal(resolveRuntimeApiUrl('/api/auth/login'), '/staging/api/auth/login')

  setApiEnvironment('development')
  assert.equal(getApiEnvironment(), 'development')
  assert.equal(getApiBaseUrl(), '/dev/api')
  assert.equal(resolveRuntimeApiUrl('/api/auth/login'), '/dev/api/auth/login')
  resetApiEnvironment()
  assert.equal(resolveRuntimeApiUrl('/api/auth/login'), '/api/auth/login')
})

test('shadow environment rejects cross-origin APIs and assets and rewrites same-origin API assets', () => {
  installSessionStorage()
  setApiEnvironment('shadow')

  assert.throws(
    () => resolveRuntimeApiUrl('https://untrusted.example/api/users'),
    /已阻止跨域 API 请求/,
  )
  assert.equal(resolveRuntimeAssetUrl('https://untrusted.example/api/auth/avatar/7'), null)
  assert.equal(
    resolveRuntimeAssetUrl('https://example.test/api/auth/avatar/7'),
    'https://example.test/shadow-api/auth/avatar/7',
  )
})

test('bootstrap identity mismatch always blocks the selected environment', () => {
  assert.doesNotThrow(() => assertApiEnvironmentIdentity('shadow', 'shadow'))
  assert.throws(
    () => assertApiEnvironmentIdentity('production', 'shadow'),
    /已阻止登录/,
  )
  assert.throws(
    () => assertApiEnvironmentIdentity('shadow', 'production'),
    /正式入口环境身份校验失败/,
  )
})

test('authenticated fetch follows shadow state and never falls back to production', async () => {
  installSessionStorage()
  setApiEnvironment('shadow')
  let requested = ''
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async (input: RequestInfo | URL) => {
      requested = String(input)
      return new Response(JSON.stringify({
        code: 'shadow_environment_offline',
        message: '影子压测环境当前未开启',
      }), { status: 503, headers: { 'Content-Type': 'application/json' } })
    },
  })

  const response = await fetchWithAuth(
    '/api/app/bootstrap',
    undefined,
    { handleUnauthorized: false, markActivity: false },
  )

  assert.equal(response.status, 503)
  assert.equal(requested, '/shadow-api/app/bootstrap')
  assert.equal(getApiEnvironment(), 'shadow')
})

test('an offline shadow backend returns the user to login without changing environment', async () => {
  const storage = installSessionStorage()
  resetUnauthorizedRedirectForTests()
  setApiEnvironment('shadow')
  window.location.pathname = '/mobile-tasks'
  window.location.href = '/mobile-tasks'
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => new Response(JSON.stringify({
      code: 'shadow_environment_offline',
      message: '影子压测环境当前未开启',
    }), { status: 503, headers: { 'Content-Type': 'application/json' } }),
  })

  const response = await fetchWithAuth('/api/auth/me')

  assert.equal(response.status, 503)
  assert.equal(window.location.href, '/login')
  assert.equal(getApiEnvironment(), 'shadow')
  assert.deepEqual(JSON.parse(storage.get('auth_exit_reason') || '{}'), {
    code: 'shadow_environment_offline',
    message: '影子压测环境当前未开启',
  })
  resetUnauthorizedRedirectForTests()
})

test('authenticated UI keeps a persistent shadow marker and environment-bound realtime route', () => {
  const authSource = readFileSync(new URL('../src/context/AuthContext.tsx', import.meta.url), 'utf8')
  const layoutSource = readFileSync(new URL('../src/components/Layout.tsx', import.meta.url), 'utf8')
  const realtimeSource = readFileSync(
    new URL('../src/components/RealtimeCoordinator.tsx', import.meta.url),
    'utf8',
  )
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')

  assert.match(authSource, /resetApiEnvironment\(\)/)
  assert.match(layoutSource, /shadow-environment-banner/)
  assert.match(layoutSource, /全部为虚构数据/)
  assert.match(layoutSource, /脱敏验证数据/)
  assert.match(layoutSource, /虚构开发数据/)
  assert.match(layoutSource, /不会写入正式业务/)
  assert.match(layoutSource, /environment !== 'production'/)
  assert.match(layoutSource, /运行编号：/)
  assert.match(styles, /\.shadow-environment-banner\s*\{/)
  assert.match(realtimeSource, /resolveRuntimeApiUrl\('\/api\/events\/stream'\)/)
  assert.match(realtimeSource, /\[environment, user\]/)
})

test('download and attachment helpers stay inside the selected shadow route', () => {
  installSessionStorage()
  setApiEnvironment('shadow')

  assert.equal(exportGridMembersUrl(), '/shadow-api/grid-members/export')
  assert.equal(policeDispatchSourceFileUrl(7), '/shadow-api/police-dispatch/batches/7/source-file')
  assert.equal(policeDispatchFeedbackUrl(7), '/shadow-api/police-dispatch/batches/7/feedback.xlsx')
  assert.equal(
    fullchainPoliceRawDownloadUrl(8),
    '/shadow-api/police-dispatch/fullchain-archive/police-raw/uploads/8/download',
  )
  assert.equal(
    fullchainArchiveDownloadUrl(9),
    '/shadow-api/police-dispatch/fullchain-archive/exports/9/download',
  )
  assert.equal(
    workflowApi.attachmentUrl(10, 'file name', true),
    '/shadow-api/workflow/tickets/10/attachments/file%20name?inline=true',
  )
})
