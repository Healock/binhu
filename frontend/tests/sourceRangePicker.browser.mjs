import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright'
import { createServer } from 'vite'

const server = await createServer({ server: { host: '127.0.0.1', port: 0 } })
await server.listen()
const origin = `http://127.0.0.1:${server.httpServer.address().port}`
const browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'msedge' } : {}) })
try {
  for (const [width, height, dark] of [[1024, 640, false], [1280, 720, false], [1280, 960, true], [1680, 1050, false], [1920, 1080, true]]) {
  const page = await browser.newPage({ viewport: { width, height } })
  let sessionReads = 0
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.clock.install({ time: new Date('2026-10-10T04:00:00Z') })
  await page.addInitScript(() => {
    window.EventSource = class extends EventTarget { close() {} }
  })
  await page.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (url.origin !== origin) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    const send = body => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
    if (url.pathname === '/api/app/bootstrap') return send({ environment: 'production', timezone: 'Asia/Shanghai', server_version: '0.30.27' })
    if (url.pathname === '/api/auth/me' || url.pathname === '/api/auth/activity') {
    if (url.pathname.endsWith('/me')) sessionReads += 1
    return send({ user: {
      id: 1, username: 'synthetic-viewer', display_name: '合成测试账号', role: 'user',
      permissions: ['visit.summary.view', 'visit.source.manage'], preferences: { theme_mode: dark ? 'dark' : 'light' },
    } })
    }
    if (url.pathname === '/api/visits/sources/status') return send({ business_date: '2026-10-10', latest_attempts: {}, current_sources: {} })
    if (url.pathname === '/api/visits/coverage') return send({ missing_dates: [], missing_date_count: 0 })
    if (url.pathname === '/api/visits/summary') return send({
      category: 'rental', category_label: '出租房', start_date: '2026-10-10', end_date: '2026-10-10',
      attendance: { complete: true, person_days: 0, missing_week_starts: [], history_started_on: null, legacy_history_incomplete: false, worked_while_off: 0, unknown_participant_days: 0 },
      overview: { visit_records: 0, participant_count: 0, person_days: 0, community_count: 0, added_count: 0, changed_count: 0, cancelled_count: 0, total_changes: 0, rated_records: 0, unrated_records: 0, rating_rate: 0 },
      inspector: { columns: [], data: [] }, community: { columns: [], data: [] },
    })
    return send({ data: [], items: [], total: 0, unread_count: 0, online_count: 1 })
  })
  await page.goto(`${origin}/visit-summary?start=2026-10-10&end=2026-10-10`, { waitUntil: 'domcontentloaded', timeout: 60_000 })
  await page.waitForTimeout(1500)
  mkdirSync('artifacts/source-range-picker', { recursive: true })
  const refreshSession = async () => {
    const before = sessionReads
    await page.clock.runFor(61_000)
    await page.waitForTimeout(150)
    assert.ok(sessionReads > before, 'exercise a real /auth/me response and context rerender')
  }
  const exercise = async (picker, name) => {
    await picker.locator('input').first().click()
    await page.waitForTimeout(200)
    const popup = page.locator('.ant-picker-dropdown:visible')
    const headers = () => popup.locator('.ant-picker-header-view').allTextContents()
    await popup.locator('.ant-picker-header-prev-btn:visible').click()
    await page.waitForTimeout(150)
    const navigated = await headers()
    assert.deepEqual(navigated, ['2026年9月', '2026年10月'])
    await refreshSession()
    assert.deepEqual(await headers(), navigated, 'refresh must not undo month navigation')
    await popup.locator('.ant-picker-month-btn').first().click()
    await refreshSession()
    assert.equal(await popup.locator('.ant-picker-month-panel').count(), 1)
    await popup.locator('td[title="2026-09"]').click()
    await popup.locator('td[title="2026-09-15"]').first().click()
    const pending = await picker.locator('input').first().inputValue()
    assert.equal(pending, '2026-09-15')
    await refreshSession()
    assert.equal(await picker.locator('input').first().inputValue(), pending, 'refresh must retain incomplete selection')
    await page.screenshot({ path: `artifacts/source-range-picker/${name}-${width}-${height}-${dark ? 'dark' : 'light'}.png` })
    await popup.locator('td[title="2026-10-03"]').first().click()
    await page.waitForTimeout(150)
    assert.deepEqual(await picker.locator('input').evaluateAll(inputs => inputs.map(input => input.value)), ['2026-09-15', '2026-10-03'])
    await page.keyboard.press('Escape')
  }
  await exercise(page.locator('.external-data-panel__controls .ant-picker'), 'source')
  await exercise(page.locator('.ant-picker').nth(1), 'summary')
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ width, height, dark, sessionReads, result: 'passed' }))
  await page.close()
  }
} finally {
  await browser.close()
  await server.close()
}
