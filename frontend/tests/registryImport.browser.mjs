import assert from 'node:assert/strict'
import { chromium } from 'playwright'
import { createServer } from 'vite'

// Synthetic responses only: exercise the page without accessing business data.
const server = await createServer({ server: { host: '127.0.0.1', port: 0 } })
await server.listen()
const origin = `http://127.0.0.1:${server.httpServer.address().port}`
let browser
try {
  browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'msedge' } : {}) })
  const cases = [
    ...[[1024, 640, false], [1280, 720, false], [1280, 960, true], [1680, 1050, false], [1920, 1080, true], [390, 844, false]]
      .map(([width, height, dark]) => ({ width, height, dark, status: 'partially_imported', certificate: false })),
    { width: 1280, height: 720, dark: false, status: 'preview', certificate: false },
    { width: 1280, height: 720, dark: false, status: 'imported', certificate: false },
    { width: 1280, height: 720, dark: false, status: 'preview', certificate: true },
    { width: 1280, height: 720, dark: false, status: 'partially_imported', certificate: true },
  ]
  for (const scenario of cases) {
    const { width, height, dark, status, certificate } = scenario
    const context = await browser.newContext({ viewport: { width, height } })
    const page = await context.newPage()
    const errors = []
    const confirmations = []
    const preview = { batch_id: 7, status, total_count: 3, normal_count: 2, issue_count: 1,
      problem_row_count: 1, file_count: 1, files: [{ file_name: 'synthetic.xlsx', total_count: 3 }] }
    page.on('pageerror', error => errors.push(error.message))
    await context.addInitScript(dark => localStorage.setItem('binhu-theme-mode', dark ? 'dark' : 'light'), dark)
    await context.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.origin !== origin) return route.abort()
      if (!url.pathname.startsWith('/api/')) return route.continue()
      const send = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
      if (url.pathname === '/api/app/bootstrap') return send({ environment: 'production', timezone: 'Asia/Shanghai', server_version: '0.31.0' })
      if (url.pathname === '/api/auth/me') return send({ user: { id: 1, username: 'synthetic-importer', display_name: '合成测试账号', role: 'user',
        permissions: ['registry.property.view', 'registry.import.manage'], permission_groups: [], preferences: { theme_mode: dark ? 'dark' : 'light' } } })
      if (url.pathname === '/api/registry/imports/certificates/source-runs/latest') return send({ data: certificate
        ? { id: 1, status: 'completed', batch_id: 7, preview, fetched_count: 3, accepted_count: 3 } : null })
      if (url.pathname === '/api/registry/imports/households/files/preview') return send(preview)
      if (/\/registry\/imports\/(households|certificates)\/7\/confirm$/.test(url.pathname)) {
        confirmations.push({ path: url.pathname, method: route.request().method() })
        if (status === 'partially_imported' && confirmations.length === 1) return send({ detail: '合成确认失败，请重试' }, 500)
        return send({ batch_id: 7, status: 'imported', imported_count: 2, inserted_count: 1, updated_count: 1, pending_issue_count: 0 })
      }
      return send({ data: [], total: 0, unread_count: 0 })
    })
    await page.goto(`${origin}/registry`)
    await page.getByRole('tab', { name: '数据导入', exact: true }).click()
    if (!certificate) {
      await page.locator('input[type=file]').setInputFiles({ name: 'synthetic.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: Buffer.from('mocked preview') })
      await page.getByRole('button', { name: '预览户号表', exact: true }).click()
    }
    await page.getByText(/(户号表|告知书)共 3 条/).waitFor()
    const buttons = page.getByRole('button', { name: /^(确认导入正常数据|继续导入正常数据|确认挂载告知书)$/ })
    if (status === 'imported' || (certificate && status === 'partially_imported')) {
      assert.equal(await buttons.count(), 0)
      if (!certificate) await page.getByText('本批数据已导入，相同文件不会重复写入。', { exact: false }).waitFor()
    } else {
      const label = certificate ? '确认挂载告知书' : status === 'preview' ? '确认导入正常数据' : '继续导入正常数据'
      const button = page.getByRole('button', { name: label, exact: true })
      await button.waitFor()
      await button.scrollIntoViewIfNeeded()
      const bounds = await button.boundingBox()
      assert.ok(bounds && bounds.x >= 0 && bounds.x + bounds.width <= width, 'confirmation button fits viewport')
      if (status === 'partially_imported') {
        await page.getByText('本批已完成部分导入', { exact: false }).waitFor()
        await button.click()
        await page.getByText('合成确认失败，请重试', { exact: true }).waitFor()
        await button.waitFor()
        assert.equal(await button.isEnabled(), true)
      }
      await button.click()
      await buttons.waitFor({ state: 'detached' })
      assert.equal(confirmations.length, status === 'partially_imported' ? 2 : 1)
      assert.ok(confirmations.every(item => item.method === 'POST' && item.path === `/api/registry/imports/${certificate ? 'certificates' : 'households'}/7/confirm`))
      if (!certificate) await page.getByText('本次更新：1', { exact: true }).waitFor()
    }
    assert.deepEqual(errors, [])
    console.log(JSON.stringify({ ...scenario, result: 'passed', confirmations: confirmations.length }))
    await context.close()
  }
} finally {
  await browser?.close()
  await server.close()
}
