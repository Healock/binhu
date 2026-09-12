import assert from 'node:assert/strict'
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
const server = await createServer({ server: { host: '127.0.0.1', port: 5199, strictPort: true } }); await server.listen()
const origin = 'http://127.0.0.1:5199'
const browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'msedge' } : {}) })
try {
 for (const [width, dark] of [[1280, false], [390, false], [1280, true], [390, true]]) {
  const context = await browser.newContext({ viewport: { width, height: 960 } }); const page = await context.newPage()
  const errors = []; page.on('pageerror', e => { errors.push(e.message); console.error(e.message) })
  let revision = 1, delay = 0, fail = '', refreshDelay = 0, failEditors = false, canEdit = true, searchMode = 'none', changeOtherField = false
  let values = { 姓名: '虚构输入任务', 核查人: '虚构核查员', 核查结果: '无法核实', 现住址: '虚构原地址', 二次反馈: '', 备注: '' }
  const writes = []
  const detail = () => ({ task: {}, writeback_enabled: canEdit, workflow: { result_field: '核查结果', secondary_fields: ['二次反馈'], analysis_fields: [], columns: [] }, sources: [{ id: 1, revision, row_key: 'fixture:1', source_available: true, values: { ...values }, editable_fields: ['核查结果', '现住址', '二次反馈', '备注'], cell_meta: { 核查结果: { type: 'select', options: [{ text: '无法核实' }, { text: '待登记' }] } } }] })
  await context.route('**/*', async route => {
   const url = new URL(route.request().url()); if (url.origin !== origin) return route.abort()
   if (!url.pathname.startsWith('/api/')) return route.continue()
   const send = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
   if (url.pathname.endsWith('/inline-editors')) {
    const snapshot = detail(); await new Promise(r => setTimeout(r, refreshDelay))
    return send(failEditors ? { detail: '模拟读取失败' } : { items: { 'fixture:1': { available: true, detail: snapshot } } }, failEditors ? 503 : 200)
   }
   if (url.pathname.includes('source-rows') && route.request().method() === 'PATCH') {
    const body = route.request().postDataJSON(); writes.push(body)
    const failure = fail; fail = ''; await new Promise(r => setTimeout(r, delay))
    if (failure === 'conflict') { values['二次反馈'] = '服务器新增线索'; revision++; return send({ detail: { code: 'task_revision_conflict', columns: ['二次反馈'], current_values: { 二次反馈: values['二次反馈'] }, current_revision: revision } }, 409) }
    if (failure === 'busy') return send({ detail: { code: 'task_save_busy' } }, 409)
    assert.equal(body.expected_revision, revision, 'queued write reads latest revision')
    if (changeOtherField) { values['现住址'] = '服务器更改的虚构地址'; changeOtherField = false }
    Object.assign(values, body.changes); revision++
    return send({ values: { ...values }, revision, message: '已保存' })
   }
   if (url.pathname.endsWith('/registry/properties/search')) return send(searchMode === 'error' ? { detail: '模拟搜索失败' } : { data: searchMode === 'one' ? [{ id: 7, version: 3, natural_address: '虚构候选地址', building: '', room: '' }] : [] }, searchMode === 'error' ? 503 : 200)
   return send({ data: [], timezone: 'Asia/Shanghai' })
  })
  const field = name => page.locator(`[data-editor-field="${name}"]`)
  const feedback = field('二次反馈').locator('textarea')
  const address = () => field('现住址').locator('textarea, input:not([type=hidden])').first()
  const waitWrites = async n => { for (let i = 0; i < 80 && writes.length < n; i++) await page.waitForTimeout(50); assert.equal(writes.length, n) }
  await page.goto(`${origin}/tests/taskAutosave.fixture.html${dark ? '?dark' : ''}`)
  await feedback.waitFor(); await feedback.scrollIntoViewIfNeeded(); await feedback.focus()
  // Starting composition cancels an already scheduled save.
  await feedback.fill('前缀'); await feedback.dispatchEvent('compositionstart'); await feedback.fill('前缀zhongwen')
  await page.waitForTimeout(900); assert.equal(writes.length, 0)
  await feedback.fill('前缀中文'); await feedback.dispatchEvent('compositionend', { data: '中文' })
  await page.waitForTimeout(250); assert.equal(writes.length, 0)
  await waitWrites(1); assert.equal(writes[0].changes['二次反馈'], '前缀中文'); await page.waitForTimeout(80)
  assert.equal(await field('现住址').getByText('已保存', { exact: true }).count(), 0)
  // Sample every rendered frame through slow saves and continued input.
  await feedback.evaluate(el => { window.originalInput = el; window.positions = []; window.sample = true; const frame = () => { if (!window.sample) return; window.positions.push([el.getBoundingClientRect().y, document.activeElement === el, el.disabled]); requestAnimationFrame(frame) }; frame() })
  delay = 500
  await feedback.fill('第一版'); await waitWrites(2); await feedback.fill('第二版')
  await page.waitForTimeout(600); assert.equal(await feedback.inputValue(), '第二版')
  await waitWrites(3); await page.waitForTimeout(600)
  const frames = await page.evaluate(() => { window.sample = false; return window.positions })
  assert.ok(frames.every(x => x[1] && !x[2]), 'focus and enabled state survive save')
  assert.ok(Math.max(...frames.map(x => x[0])) - Math.min(...frames.map(x => x[0])) <= 1, 'stable input position')
  assert.equal(await feedback.evaluate(el => el === window.originalInput), true)
  assert.equal(values['二次反馈'], '第二版')
  await feedback.fill('跨字段反馈'); await waitWrites(4); await address().fill('虚构新住址')
  await feedback.focus(); await page.waitForTimeout(1500)
  assert.equal(values['二次反馈'], '跨字段反馈'); assert.equal(values['现住址'], '虚构新住址'); assert.equal(writes.length, 5)
  refreshDelay = 1300; await page.evaluate(() => window.refreshFixture()); await feedback.fill('刷新中的草稿'); await waitWrites(6)
  await page.waitForTimeout(1600); assert.equal(await feedback.inputValue(), '刷新中的草稿'); refreshDelay = 0
  failEditors = true; await page.evaluate(() => window.refreshFixture()); await page.waitForTimeout(250)
  assert.equal(await feedback.isEnabled(), true); failEditors = false
  delay = 0; fail = 'conflict'; await feedback.fill('保留的冲突草稿'); await waitWrites(7); await page.waitForTimeout(100)
  assert.equal(await field('现住址').getByText('核对后重试').count(), 0)
  await feedback.fill('核对后的最新草稿'); await page.waitForTimeout(850); assert.equal(writes.length, 7)
  await field('二次反馈').getByRole('button', { name: '核对后重试' }).click()
  await page.getByText('服务器当前值：服务器新增线索').waitFor()
  await page.getByRole('button', { name: '保留草稿并重试' }).click(); await waitWrites(8); await page.waitForTimeout(100)
  assert.equal(values['二次反馈'], '核对后的最新草稿')
  fail = 'busy'; await feedback.fill('忙碌时草稿'); await waitWrites(9); await page.waitForTimeout(100)
  await feedback.fill(''); await page.waitForTimeout(850); assert.equal(writes.length, 9)
  await field('二次反馈').getByRole('button', { name: '重试保存' }).click(); await waitWrites(10); await page.waitForTimeout(80); assert.equal(values['二次反馈'], '')
  // Exercise Chromium's composition path, in addition to dispatched DOM events.
  const ime = await context.newCDPSession(page)
  await feedback.focus()
  await ime.send('Input.imeSetComposition', { text: '中文候选', selectionStart: 4, selectionEnd: 4 })
  await page.waitForTimeout(850); assert.equal(writes.length, 10)
  await ime.send('Input.insertText', { text: '中文最终选词' })
  await waitWrites(11); await page.waitForTimeout(100); assert.equal(values['二次反馈'], '中文最终选词')
  // A blur already queued behind another field must stop if composition starts.
  delay = 1000; await feedback.fill('队列前字段'); await waitWrites(12)
  await address().fill('排队地址'); await feedback.focus(); await address().focus(); await address().dispatchEvent('compositionstart')
  await address().fill('仍在组词'); await page.waitForTimeout(1150); assert.equal(writes.length, 12)
  await address().fill('组词后新地址'); await address().dispatchEvent('compositionend'); delay = 0
  await waitWrites(13); await page.waitForTimeout(100)
  // A stale failure cannot repaint active composition or discard the new draft.
  fail = 'busy'; delay = 700; await feedback.fill('失败请求'); await waitWrites(14)
  await feedback.dispatchEvent('compositionstart'); await feedback.fill('失败后继续组词')
  await page.waitForTimeout(850); assert.equal(await field('二次反馈').getByText('重试保存').count(), 0)
  assert.equal(await feedback.inputValue(), '失败后继续组词')
  await feedback.dispatchEvent('compositionend'); await page.waitForTimeout(800); assert.equal(writes.length, 14)
  delay = 0; await field('二次反馈').getByRole('button', { name: '重试保存' }).click(); await waitWrites(15); await page.waitForTimeout(100)
  // A full response cannot silently rebase a different dirty field.
  delay = 600; changeOtherField = true; await feedback.fill('响应携带别人的地址'); await waitWrites(16)
  await address().fill('本地保留地址'); await page.waitForTimeout(850)
  assert.equal(await address().inputValue(), '本地保留地址'); assert.equal(writes.length, 16)
  await field('现住址').getByRole('button', { name: '核对后重试' }).click()
  await page.getByText('服务器当前值：服务器更改的虚构地址').waitFor()
  delay = 0; await page.getByRole('button', { name: '保留草稿并重试' }).click(); await waitWrites(17); await page.waitForTimeout(100)
  const pendingStart = writes.length
  await field('核查结果').locator('.ant-select').click(); await page.getByTitle('待登记', { exact: true }).click()
  await waitWrites(pendingStart + 1); await page.waitForTimeout(100)
  assert.equal(writes[pendingStart].registration_pending_address, '本地保留地址'); assert.equal(writes[pendingStart].changes['核查结果'], '待登记')
  searchMode = 'error'; await address().fill('虚构无候选地址'); await page.waitForTimeout(1000); await waitWrites(pendingStart + 2)
  assert.equal(values['现住址'], '虚构无候选地址')
  searchMode = 'one'; await address().fill('虚构候选'); await page.waitForTimeout(1100); await waitWrites(pendingStart + 3)
  await address().click(); await page.getByText('虚构候选地址', { exact: true }).click(); await page.waitForTimeout(300); await waitWrites(pendingStart + 4)
  assert.equal(writes[pendingStart + 3].registration_property_id, 7); assert.equal(writes[pendingStart + 3].registration_property_version, 3)
  assert.equal(values['现住址'], '虚构候选地址')
  assert.ok(writes.every(w => !Object.values(w.changes).some(v => /^(pending:|property:|candidate-)/.test(v))))
  await page.getByRole('button', { name: '切换详情' }).click(); await page.getByRole('button', { name: '切换详情' }).click(); await page.waitForTimeout(300)
  assert.equal(await address().inputValue(), '虚构候选地址')
  assert.deepEqual(errors, [])
  mkdirSync('artifacts/autosave', { recursive: true }); await page.screenshot({ path: `artifacts/autosave/${width}-${dark ? 'dark' : 'light'}.png`, fullPage: true })
  canEdit = false; await page.evaluate(() => window.refreshFixture()); await page.waitForTimeout(250); assert.equal(await address().isEnabled(), false)
  console.log(JSON.stringify({ width, dark, writes: writes.length, maxDrift: Math.max(...frames.map(x => x[0])) - Math.min(...frames.map(x => x[0])), result: 'passed' }))
  await context.close()
 }
} finally { await browser.close(); await server.close() }
