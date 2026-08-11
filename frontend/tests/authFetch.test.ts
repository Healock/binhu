import assert from 'node:assert/strict'
import test from 'node:test'

import {
  fetchWithAuth,
  resetUnauthorizedRedirectForTests,
} from '../src/api/client.ts'

function installBrowserState() {
  const values = new Map<string, string>()
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { location: { pathname: '/users', href: '/users' } },
  })
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true,
    value: {
      setItem: (key: string, value: string) => values.set(key, value),
      getItem: (key: string) => values.get(key) ?? null,
    },
  })
  return values
}

test('fetchWithAuth stores backend exit reason and redirects on business 401', async () => {
  const values = installBrowserState()
  resetUnauthorizedRedirectForTests()
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async (_input: unknown, init: RequestInit) => {
      assert.equal(init.credentials, 'include')
      return new Response(JSON.stringify({
        detail: { code: 'session_replaced', message: '账号已在另一台设备登录' },
      }), { status: 401, headers: { 'Content-Type': 'application/json' } })
    },
  })

  await fetchWithAuth('/api/users')

  assert.equal((globalThis as any).window.location.href, '/login')
  assert.deepEqual(JSON.parse(values.get('auth_exit_reason') || '{}'), {
    code: 'session_replaced',
    message: '账号已在另一台设备登录',
  })
})

test('login 401 can bypass session-expiry redirect', async () => {
  const values = installBrowserState()
  resetUnauthorizedRedirectForTests()
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => new Response(
      JSON.stringify({ detail: '用户名或密码错误' }),
      { status: 401, headers: { 'Content-Type': 'application/json' } },
    ),
  })

  const response = await fetchWithAuth(
    '/api/auth/login',
    { method: 'POST' },
    { handleUnauthorized: false, markActivity: false },
  )

  assert.equal(response.status, 401)
  assert.equal((globalThis as any).window.location.href, '/users')
  assert.equal(values.get('auth_exit_reason'), undefined)
})

test('malformed 401 uses the shared session-expired fallback', async () => {
  const values = installBrowserState()
  resetUnauthorizedRedirectForTests()
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => new Response('gateway response', { status: 401 }),
  })

  await fetchWithAuth('/api/users')

  assert.deepEqual(JSON.parse(values.get('auth_exit_reason') || '{}'), {
    code: 'session_expired',
    message: '登录状态已失效',
  })
})

test('authenticated 503 stores maintenance reason and redirects to login', async () => {
  const values = installBrowserState()
  resetUnauthorizedRedirectForTests()
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => new Response(JSON.stringify({
      detail: { code: 'maintenance_mode', message: '平台正在维护中' },
    }), { status: 503, headers: { 'Content-Type': 'application/json' } }),
  })

  await fetchWithAuth('/api/query')

  assert.equal((globalThis as any).window.location.href, '/login')
  assert.deepEqual(JSON.parse(values.get('auth_exit_reason') || '{}'), {
    code: 'maintenance_mode',
    message: '平台正在维护中',
  })
})

test('login 503 can stay on the login page for the form to show the reason', async () => {
  const values = installBrowserState()
  resetUnauthorizedRedirectForTests()
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => new Response(
      JSON.stringify({ detail: { code: 'maintenance_mode', message: '平台正在维护中' } }),
      { status: 503, headers: { 'Content-Type': 'application/json' } },
    ),
  })

  const response = await fetchWithAuth(
    '/api/auth/login',
    { method: 'POST' },
    { handleUnauthorized: false, markActivity: false },
  )

  assert.equal(response.status, 503)
  assert.equal((globalThis as any).window.location.href, '/users')
  assert.equal(values.get('auth_exit_reason'), undefined)
})

test('authenticated writes carry the user-activity marker', async () => {
  installBrowserState()
  resetUnauthorizedRedirectForTests()
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async (_input: unknown, init: RequestInit) => {
      const headers = new Headers(init.headers)
      assert.equal(headers.get('X-User-Activity'), '1')
      assert.equal(headers.get('X-Binhu-Client-Platform'), 'web')
      assert.equal(headers.get('X-Binhu-Client-Version'), '0.0.0')
      return new Response(null, { status: 204 })
    },
  })

  await fetchWithAuth('/api/users', { method: 'POST' })
})
