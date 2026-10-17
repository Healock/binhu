import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { chromium } from 'playwright'
import { createServer } from 'vite'

const baseline = process.env.BINHU_BACKGROUND_BASELINE === '1'
const modules = ['context/AuthContext.tsx', 'components/SessionTimeoutGuard.tsx',
  'components/OnlinePresenceIndicator.tsx', 'components/RealtimeCoordinator.tsx']
const server = await createServer({
  server: { host: '127.0.0.1', port: 0 },
  plugins: baseline ? [{ name: 'baseline-session-components', enforce: 'pre',
    transform(_code, id) {
      const module = modules.find(path => id.replaceAll('\\', '/').endsWith(`/src/${path}`))
      if (module) return execFileSync('git', ['show', `origin/main:frontend/src/${module}`], { encoding: 'utf8' })
    },
  }] : [],
})
await server.listen()
const origin = `http://127.0.0.1:${server.httpServer.address().port}`
const browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'msedge' } : {}) })
try {
  for (const viewport of baseline ? [{ width: 1280, height: 720 }] : [{ width: 1280, height: 720 }, { width: 390, height: 844 }]) {
  const page = await browser.newPage({ viewport })
  page.setDefaultTimeout(10_000)
  const errors = []
  const counts = { me: 0, heartbeat: 0, activity: 0, presenceUsers: 0 }
  page.on('pageerror', error => errors.push(error.message))
  if (!baseline) await page.clock.install()
  await page.addInitScript(() => {
    window.streamBudget = { created: 0, closed: 0 }
    // Model a healthy long-lived server stream; response data refresh must not close it.
    window.EventSource = class extends EventTarget {
      constructor() {
        super()
        window.streamBudget.created += 1
        setTimeout(() => this.onopen?.(new Event('open')), 0)
      }
      close() { window.streamBudget.closed += 1 }
    }
  })
  await page.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (url.origin !== origin) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    const send = body => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
    if (url.pathname === '/api/app/bootstrap') return send({ environment: 'production', timezone: 'Asia/Shanghai', server_version: '0.30.27' })
    if (url.pathname === '/api/auth/me' || url.pathname === '/api/auth/activity') {
      counts[url.pathname.endsWith('/me') ? 'me' : 'activity'] += 1
      if (baseline && counts.me > 20) return route.abort()
      const now = Date.now()
      return send({ user: { id: 1, username: 'synthetic-viewer', display_name: '合成测试账号',
        role: 'user', permissions: ['registry.property.view', 'presence.detail.view'], preferences: {},
        session_policy: { last_activity_at: new Date(now).toISOString(), server_time: new Date(now).toISOString(),
          absolute_expires_at: new Date(now + 3_600_000).toISOString(), idle_timeout_minutes: 30, warning_seconds: 60 } } })
    }
    if (url.pathname === '/api/presence/heartbeat') { counts.heartbeat += 1; return send({ online_count: 1 }) }
    if (url.pathname === '/api/presence/users') { counts.presenceUsers += 1; return send({ users: [], online_count: 1 }) }
    return send({ data: [], items: [], total: 0, unread_count: 0, match_status_counts: {} })
  })
  await page.goto(`${origin}/registry`, { waitUntil: 'domcontentloaded', timeout: 60_000 })
  await page.locator('.app-speed-dial__main').waitFor()
  await page.waitForTimeout(1200)
  if (baseline) {
    assert.ok(counts.me > 5 && counts.heartbeat > 5, JSON.stringify(counts))
    console.log(JSON.stringify({ baseline: true, counts, stream: await page.evaluate(() => window.streamBudget) }))
  } else {
    const initialMe = counts.me
    assert.ok(initialMe >= 1 && initialMe <= 2, 'restoration is bounded, including development StrictMode')
    const initialStreams = await page.evaluate(() => window.streamBudget.created)
    assert.ok(initialStreams <= 2, 'at most the development StrictMode mount streams')
    for (let tick = 0; tick < 25; tick += 1) {
      await page.clock.runFor(5000)
      await page.waitForTimeout(30)
    }
    assert.equal(counts.me, initialMe + 2, 'only two minute checks in 125 seconds')
    assert.equal(counts.heartbeat, 5, 'one initial plus four 30-second heartbeats')
    assert.equal(await page.evaluate(() => window.streamBudget.created), initialStreams)
    const before = { ...counts }
    // Real activity updates user; neither it nor UI changes may recreate background work.
    await page.locator('.app-speed-dial__main').click()
    await page.getByRole('button', { name: '查看在线用户' }).click()
    await page.waitForTimeout(100)
    assert.equal(counts.activity, before.activity + 1)
    assert.equal(counts.me, before.me)
    assert.equal(counts.heartbeat, before.heartbeat)
    assert.equal(await page.evaluate(() => window.streamBudget.created), initialStreams)
    await page.evaluate(() => { for (let i = 0; i < 20; i += 1) window.dispatchEvent(new Event('focus')) })
    await page.waitForTimeout(100)
    assert.equal(counts.heartbeat, before.heartbeat, 'focus bursts must not bypass the heartbeat interval')
    // Hidden tabs send no periodic session or presence requests.
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await page.clock.runFor(120_000)
    await page.waitForTimeout(100)
    assert.equal(counts.me, before.me)
    assert.equal(counts.heartbeat, before.heartbeat)
    assert.equal(await page.evaluate(() => window.streamBudget.created), initialStreams)
    console.log(JSON.stringify({ viewport, counts, stream: await page.evaluate(() => window.streamBudget), result: 'passed' }))
    assert.deepEqual(errors, [])
  }
  await page.close()
  }
} finally {
  await browser.close()
  await server.close()
}
