import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright'
import { createServer } from 'vite'

const server = await createServer({ server: { host: '127.0.0.1', port: 0 } })
await server.listen()
const origin = `http://127.0.0.1:${server.httpServer.address().port}`
const browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'msedge' } : {}) })
try {
  for (const [width, height, dark] of [[1024, 640, false], [1280, 720, false], [1280, 960, true], [1680, 1050, false], [1920, 1080, true], [390, 844, false]]) {
    const context = await browser.newContext({ viewport: { width, height } })
    const page = await context.newPage()
    const requests = []
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    await context.addInitScript(dark => localStorage.setItem('binhu-theme-mode', dark ? 'dark' : 'light'), dark)
    await context.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.origin !== origin) return route.abort()
      if (!url.pathname.startsWith('/api/')) return route.continue()
      const send = body => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
      if (url.pathname === '/api/app/bootstrap') return send({ environment: 'production', timezone: 'Asia/Shanghai', server_version: '0.30.25' })
      if (url.pathname === '/api/auth/me') return send({ user: { id: 1, username: 'synthetic-viewer', display_name: '合成测试账号', role: 'user', permissions: ['registry.property.view'], permission_groups: [], preferences: { theme_mode: dark ? 'dark' : 'light' } } })
      if (url.pathname === '/api/registry/properties/search') {
        requests.push(route.request().postDataJSON())
        return send({ data: [], total: 0, page: 1, page_size: 50, match_status_counts: {} })
      }
      if (url.pathname === '/api/registry/properties/export') {
        requests.push({ export: true, ...route.request().postDataJSON() })
        return route.fulfill({ contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', body: Buffer.from('synthetic download response') })
      }
      return send({ data: [], total: 0, unread_count: 0 })
    })
    await page.goto(`${origin}/registry`)
    const select = page.locator('.ant-select').filter({ hasText: '全部走访情况' })
    await select.waitFor()
    await select.click()
    await page.locator('.ant-select-item-option').filter({ hasText: /^从未走访$/ }).click()
    await page.locator('.ant-select').filter({ hasText: '从未走访' }).waitFor()
    await page.waitForTimeout(500)
    assert.equal(requests.filter(row => !row.export).at(-1).visit_status, 'never')
    await page.getByRole('button', { name: '导出当前结果' }).click()
    await page.waitForTimeout(500)
    assert.equal(requests.find(row => row.export).visit_status, 'never')
    const selector = page.locator('.ant-select').filter({ hasText: /^从未走访$/ })
    await selector.hover()
    await selector.locator('.ant-select-clear').click()
    await page.waitForTimeout(500)
    assert.equal(requests.filter(row => !row.export).at(-1).visit_status, '')
    const bounds = await page.locator('.ant-select').filter({ hasText: '全部走访情况' }).boundingBox()
    assert.ok(bounds && bounds.width >= 150 && bounds.x >= 0 && bounds.x + bounds.width <= width)
    assert.deepEqual(errors, [])
    mkdirSync('artifacts/registry-filters', { recursive: true })
    await page.screenshot({ path: `artifacts/registry-filters/${width}-${height}-${dark ? 'dark' : 'light'}.png`, fullPage: true })
    console.log(JSON.stringify({ width, height, dark, result: 'passed' }))
    await context.close()
  }
} finally {
  await browser.close()
  await server.close()
}
