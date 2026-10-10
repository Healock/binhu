import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright'
import { createServer } from 'vite'

const server = await createServer({ server: { host: '127.0.0.1', port: 0 } })
await server.listen()
const origin = `http://127.0.0.1:${server.httpServer.address().port}`
const browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'msedge' } : {}) })
const applyItem = id => ({ property_id: id, small_community_id: 12, expected_snapshot: 'a'.repeat(64), expected_entry_snapshot: 'b'.repeat(64), preview_token: 'c'.repeat(64) })
try {
  for (const [width, height, dark, denied] of [[1280, 720, false, false], [390, 844, false, false], [1024, 640, true, false], [1280, 960, true, false], [1680, 1050, false, false], [1920, 1080, true, false], [1280, 720, false, true]]) {
    const page = await browser.newPage({ viewport: { width, height } })
    const errors = []
    const applied = []
    let exported = null
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
        permissions: ['registry.property.view', ...(denied ? [] : ['registry.property.manage'])],
        preferences: { theme_mode: dark ? 'dark' : 'light' },
      } })
      if (url.pathname.endsWith('/small-community-annotations/export')) {
        exported = route.request().postDataJSON()
        return route.fulfill({ contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', body: 'synthetic-workbook' })
      }
      if (url.pathname.endsWith('/small-community-annotations/preview')) return send({ total: 3, ready: 2, blocked: 1, review: 0, skipped: 0, items: [
        { xlsx_row: 2, property_id: 42, status: 'ready', address: '合成路1号', community: '合成社区', target_name: '合成小区', annotation_reason: '正式名称与地址相符', reason: '可确认', apply_item: applyItem(42) },
        { xlsx_row: 3, property_id: 43, status: 'ready', address: '合成路2号'.repeat(10), community: '合成社区', target_name: '合成小区', annotation_reason: '人工复核建议', reason: '可确认', replaces_manual: true, apply_item: applyItem(43) },
        { xlsx_row: 4, property_id: 44, status: 'blocked', address: '合成路3号', community: '合成社区', reason: '房屋版本已变化，请重新导出' },
      ] })
      if (url.pathname.endsWith('/small-community-annotations/apply')) {
        const payload = route.request().postDataJSON()
        applied.push(payload)
        return send({ message: '已确认', confirmed: payload.items.length })
      }
      if (url.pathname === '/api/registry/properties/search') return send({ total: 1, page: 1, page_size: 50, match_status_counts: {}, data: [{ id: 42, community_id: 8, community_name: '合成社区', natural_address: '合成路1号', status: 'active', version: 3, address_match_status: 'unmatched' }] })
      return send({ data: [], items: [], total: 0, unread_count: 0, online_count: 1 })
    })
    await page.goto(`${origin}/registry`, { waitUntil: 'domcontentloaded', timeout: 60_000 })
    await page.getByRole('button', { name: '导出当前结果' }).waitFor()
    if (denied) {
      assert.equal(await page.getByRole('button', { name: '导出小区标注' }).count(), 0)
      assert.equal(await page.getByRole('button', { name: '回导小区标注' }).count(), 0)
    } else {
      await page.getByPlaceholder('搜索地址、户号、幢室或住房类型').fill('合成')
      await page.waitForTimeout(700)
      const download = page.waitForEvent('download')
      await page.getByRole('button', { name: '导出小区标注' }).click()
      await download
      assert.equal(exported.keyword, '合成')
      assert.equal(exported.sort, 'id_desc')
      await page.getByRole('button', { name: '回导小区标注' }).click()
      const modal = page.getByRole('dialog').filter({ hasText: '小区标注回导预览' })
      await modal.locator('input[type=file]').setInputFiles({ name: 'synthetic-labels.xlsx', mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', buffer: Buffer.from('synthetic') })
      await modal.getByText('房屋版本已变化，请重新导出').waitFor()
      assert.equal(applied.length, 0, 'preview must not apply changes')
      assert.equal(await modal.getByRole('button', { name: '确认所选 1 条' }).count(), 1)
      assert.equal(await modal.getByText('替换已有人工确认', { exact: true }).count(), 1)
      mkdirSync('artifacts/property-annotation', { recursive: true })
      await page.screenshot({ path: `artifacts/property-annotation/${width}-${dark ? 'dark' : 'light'}.png`, fullPage: true })
      const box = await modal.boundingBox()
      assert.ok(box.x >= 0 && box.x + box.width <= width + 1, 'preview modal fits viewport')
      const footer = await modal.getByRole('button', { name: '确认所选 1 条' }).boundingBox()
      assert.ok(footer.y >= 0 && footer.y + footer.height <= height, 'confirmation remains visible in short viewports')
      assert.equal(await page.locator('html').getAttribute('data-theme'), dark ? 'dark' : 'light')
      await modal.getByRole('button', { name: '确认所选 1 条' }).click()
      assert.equal(applied.length, 0, 'final confirmation is required')
      await page.getByRole('button', { name: '确认应用', exact: true }).click()
      await modal.getByText('已人工确认', { exact: true }).waitFor()
      await modal.getByText('已应用 1', { exact: true }).waitFor()
      assert.equal(applied.length, 1)
      assert.equal(applied[0].confirm, true)
      assert.deepEqual(applied[0].items.map(item => item.property_id), [42], 'manual replacement and blocked records must stay untouched')
    }
    assert.deepEqual(errors, [])
    await page.close()
  }
  console.log('Property annotation export, read-only preview, permission UI, manual replacement and explicit apply checks passed')
} finally {
  await browser.close()
  await server.close()
}
