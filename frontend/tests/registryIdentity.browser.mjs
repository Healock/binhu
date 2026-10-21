import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromium } from 'playwright'
import { createServer } from 'vite'

const testDirectory = dirname(fileURLToPath(import.meta.url))
const frontendRoot = resolve(testDirectory, '..')
const repositoryRoot = resolve(frontendRoot, '..')
const server = await createServer({ root: frontendRoot, server: { host: '127.0.0.1', port: 0 } })
await server.listen()
const origin = `http://127.0.0.1:${server.httpServer.address().port}`
const browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'msedge' } : {}) })
const base = {
  community_id: 8, community_name: '合成社区', natural_address: '合成路8号301室', normalized_address: '合成路8号301室',
  housing_type: '个人出租', household_status: '未注销', version: 1, address_match_status: 'unmatched',
  visit_count: 0, latest_visit_date: null, latest_star_rating: null, latest_star_rating_at: null,
}
try {
  const screenshotDirectory = resolve(repositoryRoot, 'scratch/registry-identity')
  mkdirSync(screenshotDirectory, { recursive: true })
  for (const [width, height, dark] of [[390, 844, false], [1024, 640, true], [1280, 720, false], [1280, 960, true], [1680, 1050, false], [1920, 1080, true]]) {
    const page = await browser.newPage({ viewport: { width, height } })
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.addInitScript(() => { window.EventSource = class extends EventTarget { close() {} } })
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.origin !== origin) return route.abort()
      if (!url.pathname.startsWith('/api/')) return route.continue()
      const send = body => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
      if (url.pathname === '/api/app/bootstrap') return send({ environment: 'production', timezone: 'Asia/Shanghai', server_version: '0.31.6' })
      if (['/api/auth/me', '/api/auth/activity'].includes(url.pathname)) return send({ user: {
        id: 1, username: 'synthetic-viewer', display_name: '合成测试账号', role: 'user',
        permissions: ['registry.property.view'], theme_mode: dark ? 'dark' : 'light',
      } })
      if (url.pathname === '/api/grid-members/communities') return send([{ id: 8, name: '合成社区', is_active: true }])
      if (url.pathname === '/api/registry/properties/search') return send({ data: [
        { ...base, id: 1, natural_address: '合成路1号301室', latest_visit_date: '2026-10-10', visit_count: 1, latest_star_rating: '五星出租房' },
        { ...base, id: 2, natural_address: '合成路2号301室', visit_association_status: 'ambiguous' },
        { ...base, id: 3, natural_address: '合成路3号301室', visit_association_status: 'historical_registration', household_status: '已注销' },
        { ...base, id: 4, natural_address: '合成路4号301室', visit_association_status: 'community_unresolved' },
      ], total: 4 })
      if (url.pathname === '/api/registry/properties/2') return send({ ...base, id: 2, visit_association_status: 'ambiguous' })
      if (url.pathname === '/api/registry/properties/4') return send({
        ...base,
        id: 4,
        street: '合成街道',
        community_name: '合成社区',
        natural_address: '合成新路4号，4幢301室，测试社区第七单元东侧房屋',
        normalized_address: '合成新路4号，4幢301室，测试社区第七单元东侧房屋',
        building: '4幢',
        room: '301室',
        housing_type: '个人出租',
        residence_type: '出租',
        source_house_no: 'SYNTHETIC-CURRENT-HOUSEHOLD',
        source_updated_at: '2026-10-18T02:00:00Z',
        updated_at: '2026-10-19T02:00:00Z',
        status: 'active',
        visit_association_status: 'community_unresolved',
        visit_count: 2,
        latest_visit_date: '2026-10-10',
        certificate_summary: {
          certificate_status: 'normal_signed', certificate_status_label: '正常签署', certificate_count: 1,
          certificate_issue_count: 0, landlord_renter_relation_label: '房东与实际出租人不同',
          responsibility_identity: '普通房东', certificate_updated_at: '2026-10-17T02:00:00Z',
        },
        certificates: [],
        identity: {
          requested_id: 4,
          canonical_id: 4,
          redirected: false,
          mapping_version: 1,
          path: [4],
          historical_ids: [{
            old_property_id: 3,
            canonical_property_id: 4,
            community_id: 8,
            mapping_version: 1,
            mapped_at: '2026-10-17T02:00:00Z',
            source_house_no: 'SYNTHETIC-OLD-HOUSEHOLD',
            household_status: '已注销',
            source_count: 2,
          }],
        },
      })
      return send({ data: [], items: [], total: 0, unread_count: 0, online_count: 1 })
    })
    await page.goto(`${origin}/registry`, { waitUntil: 'domcontentloaded', timeout: 60000 })
    await page.getByText('档案重复，走访待核对', { exact: true }).waitFor()
    await page.getByText('历史户号，请查看现行档案', { exact: true }).waitFor()
    await page.getByText('社区关联待核对', { exact: true }).waitFor()
    assert.equal(await page.locator('html').getAttribute('data-theme'), dark ? 'dark' : 'light')
    const detailButtons = page.getByRole('button', { name: /详\s*情/ })
    if (await detailButtons.count() >= 4) await detailButtons.nth(3).click()
    else await page.locator('.registry-mobile-property-row').nth(3).click()
    const drawer = page.getByRole('dialog')
    await drawer.waitFor()
    await page.getByText('请档案管理员核对房屋正式社区及社区历史名称映射，修正后刷新重试。').waitFor()
    await page.getByRole('region', { name: '当前房屋档案' }).getByText('SYNTHETIC-CURRENT-HOUSEHOLD').waitFor()
    await page.getByText('当前户号有效', { exact: true }).waitFor()
    await page.getByRole('region', { name: '房屋历史沿革' }).getByText('1 条历史户号').waitFor()
    await page.getByText('历史户号（1）', { exact: true }).click()
    await page.getByText('SYNTHETIC-OLD-HOUSEHOLD', { exact: true }).waitFor()
    await page.getByText('已注销', { exact: true }).last().waitFor()
    await page.getByText('所属街道', { exact: true }).waitFor()
    const identityColumns = await drawer.locator('.registry-property-identity__facts').evaluate(
      element => getComputedStyle(element).gridTemplateColumns.split(' ').length,
    )
    assert.equal(identityColumns, width <= 720 ? 2 : 4)
    const identityOverflow = await drawer.locator('.registry-property-identity').evaluate(
      element => element.scrollWidth > element.clientWidth + 1,
    )
    assert.equal(identityOverflow, false, 'long property address must wrap within the identity panel')
    assert.equal(await page.getByText('visit_association_status', { exact: true }).count(), 0)
    await drawer.locator('.ant-drawer-body').evaluate(element => { element.scrollTop = 0 })
    await page.waitForTimeout(400)
    await page.screenshot({ path: resolve(screenshotDirectory, `${width}-${height}-${dark ? 'dark' : 'light'}.png`) })
    const overflow = await drawer.evaluate(element => element.scrollWidth > element.clientWidth + 1)
    assert.equal(overflow, false, 'new association guidance must not overflow the detail drawer')
    assert.deepEqual(errors, [])
    console.log(JSON.stringify({ width, height, dark, result: 'passed' }))
    await page.close()
  }
} finally {
  await browser.close()
  await server.close()
}
