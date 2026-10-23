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
    await page.addInitScript(darkMode => {
      localStorage.setItem('binhu-theme-mode', darkMode ? 'dark' : 'light')
      window.EventSource = class extends EventTarget { close() {} }
    }, dark)
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.origin !== origin) return route.abort()
      if (!url.pathname.startsWith('/api/')) return route.continue()
      const send = body => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
      if (url.pathname === '/api/app/bootstrap') return send({ environment: 'production', timezone: 'Asia/Shanghai', server_version: '0.31.0' })
      if (url.pathname === '/api/auth/me' || url.pathname === '/api/auth/activity') return send({ user: {
        id: 7, username: 'synthetic-admin', display_name: '合成测试账号', role: 'user',
        available_modules: ['common', 'community_police'],
        permissions: ['registry.property.view', 'registry.property.manage', 'registry.import.manage'],
        preferences: { theme_mode: dark ? 'dark' : 'light' },
      } })
      if (url.pathname.endsWith('/certificates/source-runs/latest')) return send({ data: null })
      if (url.pathname.endsWith('/households/preview')) {
        previews.push(route.request().postDataBuffer().toString('utf8'))
        return send({ batch_id: 123, status: 'preview', total_count: 10, normal_count: 10, issue_count: 0, file_count: 1,
          duplicate_row_count: 0, unique_household_count: 10, missing_household_number_count: 0,
          household_status_counts: { cancelled: 5, not_cancelled: 5, unknown: 0 },
          importable_status_counts: { cancelled: 5, not_cancelled: 5, unknown: 0 },
          files: [{ file_name: '居住房屋全量_标准.xlsx', sha256: '0'.repeat(64), total_count: 10 }] })
      }
      if (url.pathname.endsWith('/households/123/confirm')) {
        confirms.push(url.pathname)
        return send({ status: 'imported', imported_count: 10, inserted_count: 10, updated_count: 0, pending_issue_count: 0 })
      }
      return send({ data: [], items: [], total: 0, unread_count: 0, online_count: 1 })
    })
    await page.goto(`${origin}/data-upload?tab=files&panel=registry`, { waitUntil: 'domcontentloaded', timeout: 60000 })
    await page.setViewportSize({ width, height })
    await page.locator('input[type=file]').setInputFiles({
      name: '居住房屋全量_标准.xlsx',
      mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      buffer: Buffer.from('synthetic-standard-workbook'),
    })
    await page.getByRole('button', { name: '预览户号表', exact: true }).click()
    await page.getByText('唯一户号：10', { exact: true }).waitFor()
    assert.equal(previews.length, 1)
    assert.ok(previews[0].includes('name="file"'))
    assert.ok(previews[0].includes('居住房屋全量_标准.xlsx'))
    assert.equal((previews[0].match(/name="files"/g) || []).length, 0)
    assert.equal((previews[0].match(/file_options/g) || []).length, 0)
    assert.equal(confirms.length, 0, 'preview must not confirm')
    mkdirSync('artifacts/household-import', { recursive: true })
    await page.screenshot({ path: `artifacts/household-import/${width}-${dark ? 'dark' : 'light'}.png`, fullPage: true })
    await page.getByRole('button', { name: '确认导入正常数据', exact: true }).click()
    await page.getByText('本批数据已导入，相同文件不会重复写入。', { exact: false }).waitFor()
    await page.getByText('本次新增：10', { exact: true }).waitFor()
    assert.equal(confirms.length, 1, 'single file confirm uses one batch')
    assert.deepEqual(errors, [])
    await page.close()
  }
  console.log('Standard single-file household import, multipart request and confirmation passed')
} finally {
  await browser.close()
  await server.close()
}
