// The page and upload interaction are real; every API response is synthetic.
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
import assert from 'node:assert/strict'
const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright')
const origin = process.env.IMPORT_TEST_ORIGIN || 'http://127.0.0.1:5200'
const out = '../scratch/analysis-import'; mkdirSync(out, { recursive: true })
const browser = await chromium.launch({ headless: true, channel: 'msedge' })
const page = await browser.newPage({ viewport: { width: 1280, height: 720 } })
const errors = []; page.on('pageerror', error => errors.push(error.message))
let response = {}, status = 200, submissions = 0
await page.route('**/*', route => {
  const url = new URL(route.request().url())
  if (url.origin !== origin) return route.abort()
  if (!url.pathname.startsWith('/api/')) return route.continue()
  const send = (body, code = 200) => route.fulfill({ status: code, contentType: 'application/json', body: JSON.stringify(body) })
  if (url.pathname.endsWith('/auth/me')) return send({ user: { id: 999, username: 'synthetic-import', role: 'super_admin', permissions: ['online.raw.view', 'online.task.manage'], permission_groups: [], member: { position: '基础管控' }, preferences: {} } })
  if (url.pathname.endsWith('/app/bootstrap')) return send({ environment: 'production', server_version: '0.28.10', timezone: 'Asia/Shanghai' })
  if (url.pathname.endsWith('/analysis/import')) { submissions++; return send(response, status) }
  return send({ data: [], total: 0, page: 1, page_size: 20, source_ready: true, communities: [], inspectors: [], small_communities: [], facets: {} })
})
async function upload(body, code = 200) {
  response = body; status = code
  const before = submissions
  await page.locator('input[type=file]').first().setInputFiles({ name: 'synthetic-review.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: Buffer.from('synthetic transport fixture only') })
  await page.waitForFunction(() => !!document.querySelector('[aria-label="研判导入结果"]'))
  assert.equal(submissions, before + 1)
}
try {
  await page.goto(`${origin}/tests/analysisImport.fixture.html`)
  await page.getByRole('button', { name: '导入研判结果', exact: true }).waitFor()
  const panel = page.getByRole('region', { name: '研判导入结果' })
  await upload({ success_count: 0, failed_count: 1, success: [], failed: [{ row: 2, reason: { message: '任务版本已变化，请重新导出', private_body: 'must-not-render' } }] })
  await panel.getByText('第 2 行', { exact: true }).waitFor()
  await panel.getByText('任务版本已变化，请重新导出', { exact: true }).waitFor()
  assert.equal(await page.getByText('must-not-render').count(), 0)
  for (const [width, height] of [[1024,640], [1280,720], [390,844]]) for (const dark of [false,true]) {
    await page.setViewportSize({width,height}); await page.evaluate(v => window.setFixtureTheme(v), dark)
    await panel.getByRole('button', { name: '重新导入修正后的文件' }).scrollIntoViewIfNeeded()
    await panel.getByRole('button', { name: '重新导入修正后的文件' }).click({ trial: true })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'page must not overflow horizontally')
    await page.screenshot({ path: `${out}/${width}-${dark}.png`, fullPage: true })
  }
  await panel.getByRole('button', { name: '关闭结果' }).click(); assert.equal(await panel.count(), 0)
  await upload({ success_count: 0, failed_count: 0, success: [], failed: [] })
  await panel.getByText('没有导入任何研判结果', { exact: true }).waitFor()
  await upload({ detail: { code: 'synthetic' } }, 503)
  await panel.getByText('导入未完成', { exact: true }).waitFor()
  await upload({ success_count: 1, failed_count: 0, success: [{ row: 2, task: 'synthetic', state: 'initial_extension' }], failed: [] })
  await panel.getByText('已填写的研判结果已导入', { exact: true }).waitFor()
  assert.deepEqual(errors, [])
  writeFileSync(`${out}/result.json`, JSON.stringify({ passed: true, submissions, errors }, null, 2))
  console.log('PASS import row diagnostics, structured error, blank decisions, transport error, retry, close, responsive themes')
} finally { await browser.close() }
