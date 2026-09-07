// Real Modal, ResizeObserver and virtual rows; all business data is fictional.
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
import assert from 'node:assert/strict'
const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright')
const origin = process.env.ASSIGNMENT_TEST_ORIGIN || 'http://127.0.0.1:5198'
const out = '../scratch/assignment-grid'
mkdirSync(out, { recursive: true })
const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' })
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } })
const errors = [], evidence = []
page.on('pageerror', error => errors.push(error.message))
const data = Array.from({ length: 137 }, (_, index) => ({
  row_key: `synthetic-${index}`, community: '虚构社区甲', source: '自主申报',
  address: `虚构测试路${String(index).padStart(3, '0')}号 ${index % 7 === 0 ? '仅用于布局测试的很长地址描述'.repeat(6) : '虚构楼栋101室'}`,
  small_community: index % 2 ? '虚构小区甲' : '虚构小区乙', match_status: 'suggested',
}))
await page.route('**/*', route => {
  const url = new URL(route.request().url())
  if (url.origin !== origin) return route.abort()
  if (!url.pathname.startsWith('/api/')) return route.continue()
  assert.equal(route.request().method(), 'GET', 'Layout tests must not submit assignment writes')
  return route.fulfill({ contentType: 'application/json', body: JSON.stringify({
    data, total: data.length, communities: [{ value: '虚构社区甲', label: '虚构社区甲', count: data.length }],
    inspectors_by_community: { '虚构社区甲': ['虚构核查员甲'] }, inspector_counts_by_community: {}, limited: false, limit: 2000,
  }) })
})
const cards = page.locator('.mobile-task-assignment-item')
async function checkGrid(width, height) {
  await page.setViewportSize({ width, height })
  await page.waitForTimeout(350) // Let the real Modal enter animation and ResizeObserver settle.
  await page.waitForFunction(() => {
    const scroll = document.querySelector('.mobile-task-assignment-workbench__scroll')
    const row = document.querySelector('.mobile-task-assignment-virtual-row')
    return row && row.children.length === Math.max(1, Math.floor((scroll.clientWidth + 10) / 260))
  }, null, { timeout: 5000 })
  const result = await page.evaluate(() => {
    const scroll = document.querySelector('.mobile-task-assignment-workbench__scroll')
    const rows = [...document.querySelectorAll('.mobile-task-assignment-virtual-row')]
    const first = [...rows[0].children].map(el => el.getBoundingClientRect().toJSON())
    const boxes = rows.map(el => el.getBoundingClientRect().toJSON())
    return { columns: first.length, width: scroll.clientWidth, first,
      scrollBox: scroll.getBoundingClientRect().toJSON(), viewportHeight: innerHeight,
      overlap: boxes.slice(1).some((box, index) => box.top < boxes[index].bottom - 1),
      overflow: scroll.scrollWidth > scroll.clientWidth + 1 }
  })
  assert.equal(result.overflow, false, 'No horizontal overflow')
  assert.ok(result.scrollBox.height >= 100, 'Toolbar must leave a usable task area')
  assert.ok(result.scrollBox.bottom <= result.viewportHeight + 1, 'Task scroll area stays inside the window')
  assert.equal(result.overlap, false, 'Measured virtual rows must not overlap')
  for (let index = 1; index < result.first.length; index++) {
    assert.ok(Math.abs(result.first[index].top - result.first[0].top) < 1, 'Cards share one visual row')
    assert.ok(result.first[index].left >= result.first[index - 1].right + 9, 'Columns have visible gaps')
  }
  evidence.push({ width, height, columns: result.columns })
}
try {
  await page.goto(`${origin}/tests/assignmentGrid.fixture.html`)
  await page.getByRole('button', { name: '打开虚构分配工作台' }).click()
  await cards.first().waitFor()
  await checkGrid(1920, 1080) // Fails on main: lazy Modal mount leaves columnCount at 1.
  const first = await cards.nth(0).boundingBox(), third = await cards.nth(2).boundingBox()
  await page.mouse.move(first.x + 25, first.y + 25); await page.mouse.down()
  await page.mouse.move(third.x + 25, third.y + 25, { steps: 24 }); await page.mouse.up()
  assert.equal(await page.locator('.mobile-task-assignment-item.is-selected').count(), 3)
  await cards.nth(1).focus(); await page.keyboard.press('Space')
  assert.equal(await cards.nth(1).getAttribute('aria-pressed'), 'false')
  for (const [width, height] of [[768, 900], [1024, 640], [1280, 720], [1280, 960], [1680, 1050], [1920, 1080], [1536, 864], [390, 844]]) {
    for (const dark of [false, true]) {
      await page.evaluate(value => window.setFixtureTheme(value), dark)
      await checkGrid(width, height)
      await page.getByRole('button', { name: '分配核查人', exact: true }).click({ trial: true })
      assert.equal(await page.locator('.mobile-task-assignment-item.is-selected').count(), 2, 'Resize preserves selected tasks')
      await page.screenshot({ path: `${out}/${width}x${height}-${dark ? 'dark' : 'light'}.png`, fullPage: true })
    }
  }
  await page.getByRole('button', { name: '清空选择', exact: true }).click()
  await page.locator('.mobile-task-assignment-workbench__scroll').evaluate(el => { el.scrollTop = el.scrollHeight })
  await page.getByText(data.at(-1).address, { exact: true }).waitFor()
  await page.getByRole('button', { name: /退出分配/ }).click()
  await page.getByRole('button', { name: '打开虚构分配工作台' }).click()
  await checkGrid(1920, 1080)
  assert.deepEqual(errors, [])
  writeFileSync(`${out}/result.json`, JSON.stringify({ passed: true, evidence, errors }, null, 2))
  console.log('PASS first-open/reopen, responsive columns, drag and keyboard selection, long-address row measurements, last task')
} catch (error) {
  await page.screenshot({ path: `${out}/failure.png`, fullPage: true })
  throw error
} finally { await browser.close() }
