import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { mkdirSync } from 'node:fs'
const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright')
const origin = process.env.TEST_ORIGIN || 'http://127.0.0.1:5198'
const browser = await chromium.launch({ channel: 'msedge', headless: true })
const context = await browser.newContext({ viewport: { width: 1280, height: 960 } })
const page = await context.newPage()
const errors = []
page.on('pageerror', error => errors.push(error.message))
let searches = 0, editors = 0, removed = 0, failSearch = false, revision = 1, canEdit = true, failEditors = false, failSave = false
const writes = []
const task = id => ({ task_key: `全链条:fixture-${id}`, row_key: `fixture-${id}`, parser_type: '全链条',
  summary: { title: `虚构任务${id}`, original_address: '虚构测试路，仅用于自动化验收', current_address: '', result: '无法核实', secondary_feedback: '', analysis: '', phone: '', identity_number: '', date: '2026-09-11', deadline: '2026-09-13' },
  state: 'checked', community: '虚构社区', inspector: '虚构网格员', needs_review: false, source_count: 1, conflict: false, pending_sync: false, priority: 'ordinary', watch_marks: [], sync_state: 'synced', review_stage: '', qmf_status: null,
})
const detail = id => ({ task: task(id), data_source_mode: 'local', writeback_enabled: canEdit, editable: canEdit,
  workflow: { parser_type: '全链条', result_field: '核查结果', title_fields: ['姓名'], date_fields: ['截止日期'], phone_fields: [], identity_fields: [], source_fields: [], address_fields: ['现住址'], analysis_fields: ['研判'], secondary_fields: ['二次反馈'], columns: [] },
  sources: [{ id, revision, row_hash: `fixture-hash-${revision}`, row_key: `fixture-${id}`, source_available: true, state: 'checked', values: { 姓名: `虚构任务${id}`, 核查结果: '无法核实', 现住址: '虚构测试地址', 二次反馈: revision > 1 ? '虚构新的核查反馈' : '' }, editable_fields: ['核查结果', '现住址', '二次反馈'], cell_meta: { 核查结果: { type: 'select', options: ['无法核实', '待登记'] } }, source_kind: 'local_table' }], events: [], writeback: null,
})
await context.route('**/*', async route => {
  const url = new URL(route.request().url())
  if (url.origin !== origin) return route.abort()
  if (/^\/tasks(?:\/|$)/.test(url.pathname)) return route.fulfill({ response: await route.fetch({ url: `${origin}/tests/taskReturn.fixture.html` }) })
  if (!url.pathname.startsWith('/api/')) return route.continue()
  const path = decodeURIComponent(url.pathname)
  const send = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  if (path === '/api/app/bootstrap') return send({ environment: 'production', server_version: '0.28.15', timezone: 'Asia/Shanghai' })
  if (path === '/api/auth/me') return send({ user: { id: 99999, username: 'synthetic-return', display_name: '虚构测试账号', role: 'member', task_display_mode: 'table', permissions: ['online.raw.view'], permission_groups: [], member: { position: '组员', name: '虚构网格员' } } })
  if (path.endsWith('/filter-options')) return send({ communities: [], small_communities: [], inspectors: [], results: [], assignment: { enabled: false } })
  if (path.endsWith('/search') && path.includes('/mobile-tasks/')) {
    searches++
    await new Promise(resolve => setTimeout(resolve, 350))
    if (failSearch) return send({ detail: '虚构刷新失败' }, 503)
    const body = route.request().postDataJSON()
    const all = Array.from({ length: 100 }, (_, i) => i + 1).filter(id => id !== removed)
    return send({ data: all.slice((body.page - 1) * 50, body.page * 50).map(task), total: all.length, page: body.page, source_ready: true })
  }
  if (path.endsWith('/inline-editors')) {
    editors++
    const keys = route.request().postDataJSON().row_keys
    await new Promise(resolve => setTimeout(resolve, 450))
    if (failEditors) return send({ detail: '虚构填写项暂不可用' }, 503)
    return send({ items: Object.fromEntries(keys.map(key => [key, { available: true, detail: detail(Number(key.split('-')[1])) }])) })
  }
  if (path.match(/\/mobile-tasks\/全链条\/(?:\d+|fixture-\d+)$/) && route.request().method() !== 'GET') {
    const body = route.request().postDataJSON()
    writes.push(body)
    if (failSave) return send({ detail: '虚构版本冲突，草稿保留' }, 409)
    revision++
    return send({ values: { ...detail(15).sources[0].values, ...body.changes }, revision, message: '已保存' })
  }
  const match = path.match(/\/mobile-tasks\/全链条\/fixture-(\d+)$/)
  if (match) return send(detail(Number(match[1])))
  if (path.includes('notifications')) return send({ data: [], total: 0, unread_count: 0 })
  return send({ data: [], items: [], total: 0 })
})
try {
  await page.goto(`${origin}/tasks?type=全链条${process.env.BASELINE ? '&baseline=1' : ''}`)
  const row = page.locator('.mobile-task-table-primary-row[data-mobile-task-row-key="全链条:fixture-15"]')
  await row.waitFor()
  await row.scrollIntoViewIfNeeded()
  await page.waitForTimeout(1500)
  await row.scrollIntoViewIfNeeded()
  await page.waitForTimeout(500)
  const before = await row.boundingBox()
  await row.evaluate(el => { window.retainedRow = el; window.beforeTop = el.getBoundingClientRect().top })
  await row.dblclick()
  await page.getByRole('button', { name: /返\s*回$/ }).waitFor()
  const counts = [searches, editors]
  await page.waitForTimeout(900)
  assert.deepEqual([searches, editors], counts, 'hidden list does not request data')
  await page.evaluate(() => {
    window.framesSeen = []
    const sample = () => {
      const row = window.retainedRow
      if (row?.getClientRects().length) window.framesSeen.push(row.getBoundingClientRect().top)
      window.sampleId = requestAnimationFrame(sample)
    }; sample()
  })
  await page.getByRole('button', { name: /返\s*回$/ }).click()
  await row.waitFor()
  await page.waitForTimeout(1700)
  assert.equal(await row.evaluate(el => el === window.retainedRow), true, 'same DOM row and editor instance survives return')
  const frames = await page.evaluate(() => { cancelAnimationFrame(window.sampleId); return window.framesSeen })
  assert.ok(frames.length > 0)
  assert.ok(Math.max(...frames.map(top => Math.abs(top - before.y))) <= 4, `anchor drift: ${JSON.stringify({ before: before.y, min: Math.min(...frames), max: Math.max(...frames) })}`)
  assert.equal(await page.locator('[data-task-list-retained] .ant-skeleton').count(), 0)
  await row.dblclick()
  await page.getByRole('button', { name: /返\s*回$/ }).waitFor()
  failSearch = true
  await page.goBack()
  await page.getByText('刷新失败，已保留原列表。', { exact: false }).waitFor()
  assert.ok(await row.isVisible())
  failSearch = false
  await page.locator('.mobile-task-return-status').getByRole('button', { name: /重\s*试/ }).click()
  await page.waitForTimeout(700)
  // Fresh detail/version data merges into retained editors; permissions are checked again.
  await row.dblclick()
  await page.getByRole('button', { name: /返\s*回$/ }).waitFor()
  revision++
  canEdit = false
  await page.goBack()
  await page.waitForTimeout(1100)
  const fields = page.locator('[data-mobile-task-editor-row-key="全链条:fixture-15"] fieldset')
  assert.equal(await fields.evaluate(el => el.disabled), true)
  assert.ok((await page.locator('[data-mobile-task-editor-row-key="全链条:fixture-15"]').innerText()).includes('二次反馈'))
  canEdit = true
  // Removal uses the nearest surviving task and a readable notice.
  await row.dblclick()
  await page.getByRole('button', { name: /返\s*回$/ }).waitFor()
  removed = 15
  await page.goBack()
  await page.getByText('原任务已不在当前筛选中，已定位到附近任务。').waitFor()
  const nextRow = page.locator('.mobile-task-table-primary-row[data-mobile-task-row-key="全链条:fixture-16"]')
  assert.ok((await nextRow.boundingBox()).y < 960)
  // Manual scrolling ends anchor compensation.
  await page.mouse.move(950, 600)
  await page.mouse.wheel(0, 400)
  await page.waitForTimeout(400)
  const scrolled = await page.locator('main').evaluate(el => el.scrollTop)
  await page.waitForTimeout(700)
  assert.ok(Math.abs(await page.locator('main').evaluate(el => el.scrollTop) - scrolled) <= 4)
  removed = 0

  const measurements = []
  const out = process.env.TEST_OUTPUT
  if (out) mkdirSync(out, { recursive: true })
  for (const [width, height] of [[1024,640],[1280,720],[1280,960],[1680,1050],[1920,1080],[390,844]]) {
    for (const dark of [false, true]) {
      await page.setViewportSize({ width, height })
      await page.goto(`${origin}/tasks?type=全链条${dark ? '&dark=1' : ''}`)
      const selector = width < 768 ? '.mobile-task-item-card' : '.mobile-task-table-primary-row'
      const multiPage = width === 1280 && height === 960 && !dark
      if (multiPage) {
        await page.locator('.mobile-task-table-primary-row').first().waitFor()
        await page.locator('main').evaluate(el => {
          el.dispatchEvent(new WheelEvent('wheel', { deltaY: 100 }))
          el.scrollTop = el.scrollHeight
        })
      }
      const target = page.locator(`${selector}[data-mobile-task-row-key="全链条:fixture-15"]`)
      await target.waitFor()
      await target.scrollIntoViewIfNeeded()
      await page.waitForTimeout(1200)
      await target.scrollIntoViewIfNeeded()
      await page.waitForTimeout(200)
      const top = (await target.boundingBox()).y
      await target.evaluate(el => { window.retainedRow = el })
      if (width < 768) await target.click({ position: { x: 5, y: 5 } })
      else await target.dblclick()
      await page.getByRole('button', { name: /返\s*回$/ }).waitFor()
      await page.evaluate(() => {
        window.framesSeen = []
        const sample = () => { if (window.retainedRow?.getClientRects().length) window.framesSeen.push(window.retainedRow.getBoundingClientRect().top); window.sampleId = requestAnimationFrame(sample) }; sample()
      })
      await page.goBack()
      await page.waitForTimeout(1200)
      const seen = await page.evaluate(() => { cancelAnimationFrame(window.sampleId); return window.framesSeen })
      assert.ok(seen.length > 0, `retained frame ${width} ${dark}`)
      const drift = Math.max(...seen.map(value => Math.abs(value - top)))
      assert.ok(drift <= 4, `${width}x${height}, dark=${dark}: ${drift}px drift`)
      measurements.push({ width, height, dark, drift, multiPage })
      if (out) await page.screenshot({ path: `${out}/${width}x${height}-${dark ? 'dark' : 'light'}.png` })
    }
  }
  // Direct deep link creates no background list, and still resolves detail params.
  const count = searches
  await page.goto(`${origin}/tasks/全链条/fixture-15?scope=mine`)
  await page.getByRole('button', { name: /返\s*回$/ }).waitFor()
  assert.equal(searches, count)
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ passed: true, searches, editors, frames: frames.length, maxDrift: Math.max(...frames.map(top => Math.abs(top - before.y))), measurements }))
} catch (error) {
  console.error(errors, (await page.locator('body').innerText()).slice(0, 250))
  throw error
} finally { await browser.close() }
