import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  assertApiEnvironmentIdentity,
  environmentForUsername,
  getApiBaseUrl,
  getApiEnvironment,
  resetApiEnvironment,
  resolveRuntimeAssetUrl,
  resolveRuntimeApiUrl,
  setApiEnvironment,
} from '../src/utils/apiEnvironment.ts'
import { fetchWithAuth } from '../src/api/client.ts'

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

test('shadow suffix selects the shadow environment without fuzzy matching', () => {
  assert.equal(environmentForUsername('observer@shadow'), 'shadow')
  assert.equal(environmentForUsername(' Observer@Shadow '), 'shadow')
  assert.equal(environmentForUsername('shadow-observer'), 'production')
})

test('shadow environment stays in session storage and resolves only the fixed path', () => {
  installSessionStorage()
  setApiEnvironment('shadow')
  assert.equal(getApiEnvironment(), 'shadow')
  assert.equal(getApiBaseUrl(), '/shadow-api')
  assert.equal(resolveRuntimeApiUrl('/api/auth/login'), '/shadow-api/auth/login')
  resetApiEnvironment()
  assert.equal(getApiEnvironment(), 'production')
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
        detail: { code: 'shadow_environment_offline', message: '影子压测环境当前未开启' },
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
  assert.match(layoutSource, /全部为虚构数据 · 不会写入正式业务/)
  assert.match(layoutSource, /运行编号：/)
  assert.match(styles, /\.shadow-environment-banner\s*\{/)
  assert.match(realtimeSource, /resolveRuntimeApiUrl\('\/api\/events\/stream'\)/)
  assert.match(realtimeSource, /\[environment, user\]/)
})
