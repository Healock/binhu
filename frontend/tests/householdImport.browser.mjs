import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright'
import { createServer } from 'vite'

const server = await createServer({ server: { host: '127.0.0.1', port: 0 } })
await server.listen()
const origin = `http://127.0.0.1:${server.httpServer.address().port}`
const browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'msedge' } : {}) })
try {
  for (const [width, height, dark] of [[1280, 720, false], [390, 844, false], [1024, 640, true], [1280, 960, true], [1680, 1050, false], [1920, 1080, true]]) {
    const page = await browser.newPage({ viewport: { width: 1920, height } })
    const errors = []
    const previews = []
    const confirms = []
    page.on('pageerror', error => errors.push(error.message))
    await page.addInitScript(dark => {
      localStorage.setItem('binhu-theme-mode', dark ? 'dark' : 'light')
      window.EventSource = class extends EventTarget { close() {} }
    }, dark)
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.origin !== origin) return route.abort()
      if (!url.pathname.startsWith('/api/')) return route.continue()
      const send = body => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
      if (url.pathname === '/api/app/bootstrap') return send({ environment: 'production', timezone: 'Asia/Shanghai', server_version: '0.30.28' })
      if (url.pathname === '/api/auth/me' || url.pathname === '/api/auth/activity') return send({ user: {
        id: 7, username: 'synthetic-admin', display_name: '合成测试账号', role: 'user',
        permissions: ['registry.property.view', 'registry.property.manage', 'registry.import.manage'],
        preferences: { theme_mode: dark ? 'dark' : 'light' },
      } })
      if (url.pathname.endsWith('/certificates/source-runs/latest')) return send({ data: null })
      if (url.pathname.endsWith('/households/files/preview')) {
        previews.push(route.request().postDataBuffer().toString('utf8'))
        return send({ batch_id: 123, status: 'preview', total_count: 10, normal_count: 10, issue_count: 0, file_count: 10,
          duplicate_row_count: 0, unique_household_count: 10, missing_household_number_count: 0,
          household_status_counts: { cancelled: 5, not_cancelled: 5, unknown: 0 },
          importable_status_counts: { cancelled: 5, not_cancelled: 5, unknown: 0 },
          files: Array.from({ length: 10 }, (_, i) => ({ file_name: `synthetic-${i + 1}.xlsx`, sha256: String(i).padStart(64, '0'), total_count: 1,
            housing_type: '', household_status: '', expected_count: i === 0 ? 1 : null })) })
      }
      if (url.pathname.endsWith('/households/123/confirm')) {
        confirms.push(url.pathname)
        return send({ status: 'imported', imported_count: 10, inserted_count: 10, updated_count: 0, pending_issue_count: 0 })
      }
      return send({ data: [], items: [], total: 0, unread_count: 0, online_count: 1 })
    })
    await page.goto(`${origin}/registry`, { waitUntil: 'domcontentloaded', timeout: 60000 })
    await page.getByRole('tab', { name: '数据导入', exact: true }).click()
    await page.setViewportSize({ width, height })
    await page.locator('input[type=file]').setInputFiles(Array.from({ length: 10 }, (_, i) => ({
      name: `synthetic-${i + 1}.xlsx`, mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: Buffer.from(`synthetic-${i}`),
    })))
    const firstState = page.getByRole('combobox', { name: 'synthetic-1.xlsx注销状态', exact: true })
    await firstState.click()
    await page.locator('.ant-select-item-option').filter({ hasText: /^已注销（是）$/ }).click()
    await page.getByRole('spinbutton', { name: 'synthetic-1.xlsx查询总数', exact: true }).fill('1')
    await page.getByRole('button', { name: '预览户号表', exact: true }).click()
    await page.getByText('唯一户号：10', { exact: true }).waitFor()
    assert.equal(previews.length, 1)
    assert.ok(previews[0].includes('"household_status":"cancelled"'))
    assert.ok(previews[0].includes('"expected_count":1'))
    assert.equal((previews[0].match(/name="files"/g) || []).length, 10)
    assert.equal(confirms.length, 0, 'preview must not confirm')
    mkdirSync('artifacts/household-import', { recursive: true })
    await page.screenshot({ path: `artifacts/household-import/${width}-${dark ? 'dark' : 'light'}.png`, fullPage: true })
    await firstState.click()
    await page.locator('.ant-select-item-option').filter({ hasText: /^未注销（否）$/ }).click()
    if (errors.length) throw new Error(`Browser errors: ${errors.join('; ')}`)
    assert.equal(await page.getByRole('button', { name: '确认导入正常数据', exact: true }).count(), 0, 'declaration change invalidates preview')
    await page.getByRole('button', { name: /预览户号表$/ }).click()
    await page.getByText('唯一户号：10', { exact: true }).waitFor()
    assert.equal(previews.length, 2)
    assert.ok(previews[1].includes('"household_status":"not_cancelled"'))
    await page.getByRole('button', { name: '确认导入正常数据', exact: true }).click()
    await page.getByText('处理状态：imported', { exact: true }).waitFor()
    await page.getByText('本次新增：10', { exact: true }).waitFor()
    assert.equal(confirms.length, 1, 'all files confirm in a single batch')
    assert.deepEqual(errors, [])
    await page.close()
  }
  console.log('Household multi-file declarations, multipart request, preview invalidation and one-batch confirmation passed')
} finally {
  await browser.close()
  await server.close()
}
