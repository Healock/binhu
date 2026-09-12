import assert from 'node:assert/strict'
import { chromium } from 'playwright'
import { createServer } from 'vite'

const server = await createServer({ server: { host: '127.0.0.1', port: 5207, strictPort: true } })
await server.listen()
const browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'msedge' } : {}) })
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('http://127.0.0.1:5207/tests/queryEditorScroll.fixture.html')
  await page.locator('.query-spreadsheet canvas').first().waitFor()
  await page.waitForTimeout(500)
  await page.getByRole('tab', { name: '数据' }).click()
  const nativeFindButton = page.locator('[data-u-command="ui.operation.open-find-dialog"]')
  assert.ok((await nativeFindButton.count()) >= 1)
  const info = await page.evaluate(() => ({
    canvases: document.querySelectorAll('.query-spreadsheet canvas').length,
    inputs: document.querySelectorAll('.query-spreadsheet input, .query-spreadsheet textarea').length,
  }))
  assert.ok(info.canvases > 0)
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ ...info, nativeFindButton: true, result: 'fixture-mounted' }))
} finally {
  await browser.close()
  await server.close()
}
