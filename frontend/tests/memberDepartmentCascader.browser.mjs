import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright'
import { createServer } from 'vite'

const server = await createServer({ server: { host: '127.0.0.1', port: 0 } })
let browser
try {
  await server.listen()
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`
  browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'msedge' } : {}) })
  const output = 'artifacts/member-department-cascader'
  mkdirSync(output, { recursive: true })
  for (const [width, height, dark] of [[1024, 640, false], [1280, 720, false], [1280, 960, true], [1680, 1050, false], [1920, 1080, true], [390, 844, false]]) {
    const context = await browser.newContext({ viewport: { width, height } })
    const errors = []
    await context.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort())
    const page = await context.newPage()
    page.on('pageerror', error => errors.push(error.message))
    await page.goto(`${origin}/tests/memberDepartmentCascader.fixture.html${dark ? '?dark' : ''}`)
    const field = page.locator('.member-department-cascader')
    const item = text => page.locator('.ant-cascader-menu-item-content').getByText(text, { exact: true })
    const selected = page.getByTestId('department-ids')
    await field.click()
    await item('测试单位').click()
    await item('测试东片').click()
    await item('测试甲社区').click()
    await page.waitForFunction(() => document.querySelector('output')?.textContent === '[3]')
    await item('测试乙社区').click()
    await page.waitForFunction(() => document.querySelector('output')?.textContent === '[3,4]')
    // Completing one branch must not submit the parent unit or area as an ID.
    assert.equal(await selected.textContent(), '[3,4]')
    await item('测试西片').click()
    await item('测试丙社区').click()
    await page.waitForFunction(() => document.querySelector('output')?.textContent === '[3,4,6]')
    assert.equal(await page.getByText(/（社区）|（单位）/).count(), 0)
    const popup = page.locator('.member-department-cascader-popup').filter({ visible: true })
    const bounds = await popup.boundingBox()
    assert.ok(bounds && bounds.x >= -1 && bounds.x + bounds.width <= width + 1, JSON.stringify(bounds))
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
    await page.screenshot({ path: `${output}/${width}-${height}-${dark ? 'dark' : 'light'}.png` })
    await page.keyboard.press('Escape')
    await page.getByRole('button', { name: 'unit', exact: true }).click()
    await field.click()
    await item('测试单位').click()
    await page.waitForFunction(() => document.querySelector('output')?.textContent === '[1]')
    await page.keyboard.press('Escape')
    await page.getByRole('button', { name: 'community', exact: true }).click()
    await field.click()
    await item('测试单位').click()
    assert.equal(await selected.textContent(), '[]')
    await item('测试东片').click()
    assert.equal(await selected.textContent(), '[]')
    await item('测试甲社区').click()
    await page.waitForFunction(() => document.querySelector('output')?.textContent === '[3]')
    assert.deepEqual(errors, [])
    await context.close()
    console.log(`PASS ${width}x${height} ${dark ? 'dark' : 'light'}`)
  }
} finally {
  await browser?.close()
  await server.close()
}
