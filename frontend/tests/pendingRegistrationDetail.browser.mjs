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
  for (const [width, height, dark, scale = 1] of [[1024, 640, false], [1280, 720, false], [1280, 960, false], [1680, 1050, false], [1920, 1080, false, 1.25], [390, 844, false], [1280, 720, true], [390, 844, true]]) {
    const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: scale })
    page.setDefaultTimeout(10_000)
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    let revision = 1, delay = 0, failSearch = false, failSave = false
    let values = { 姓名: '合成测试任务', 核查人: '合成核查员', 核查结果: '无法核实', 现住址: '自填测试地址', 核查反馈: '合成房屋线索', 备注: '' }
    let link = null
    const property = { id: 23, version: 2, natural_address: '合成档案地址', building: '', room: '', community_name: '合成社区' }
    const writes = []
    const searches = []
    const detail = () => ({
      data_source_mode: 'local', writeback_enabled: true, dependency_blocked: false,
      task: { parser_type: '全链条', state: 'checked', source_count: 1, conflict: false, community: '合成社区', inspector: '合成核查员', summary: {}, watch_marks: [] },
      workflow: { result_field: '核查结果', title_fields: ['姓名'], date_fields: [], phone_fields: [], identity_fields: [], source_fields: [], address_fields: ['现住址'], analysis_fields: [], secondary_fields: ['核查反馈'], extra_edit_fields: ['备注'], columns: [] },
      registration_link: link,
      sources: [{ id: 11, row_key: 'fixture-row', row_hash: 'synthetic-hash', revision, state: 'checked', source_available: true, values: { ...values }, editable_fields: ['现住址', '核查反馈', '核查结果', '备注'], sync_fields: [], cell_meta: { 核查结果: { type: 'select', options: [{ text: '待登记' }, { text: '无需登记' }, { text: '无法核实' }] } } }],
      photo_requests: [],
    })
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.origin !== origin) return route.abort()
      if (!url.pathname.startsWith('/api/')) return route.continue()
      const send = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
      if (url.pathname === '/api/app/bootstrap') return send({ environment: 'production', timezone: 'Asia/Shanghai' })
      if (url.pathname === '/api/auth/me') return send({ user: { id: 1, role: 'user', username: 'synthetic-viewer', permissions: [], preferences: {}, member: { position: '组员' } } })
      if (url.pathname.endsWith('/registry/properties/search')) {
        searches.push(route.request().postDataJSON())
        return failSearch ? send({ detail: '合成搜索故障' }, 503) : send({ data: [property], total: 1 })
      }
      if (url.pathname.includes('/source-rows/') && route.request().method() === 'PATCH') {
        const body = route.request().postDataJSON()
        writes.push(body)
        assert.equal(body.expected_revision, revision)
        if (failSave) { failSave = false; return send({ detail: { code: 'task_save_busy' } }, 409) }
        Object.assign(values, body.changes)
        link = body.registration_property_id ? { status: 'awaiting_match', property_id: 23, property_version: 2, property, match_count: 0 } : body.registration_pending_address ? { status: 'pending_establishment', property_id: null, property_version: null, property: null } : null
        revision += 1
        const response = { ...detail().sources[0], values: { ...values }, registration_link: link, message: '已保存' }
        await new Promise(resolve => setTimeout(resolve, delay))
        return send(response)
      }
      if (url.pathname.includes('/mobile-tasks/')) return send(detail())
      return send({ data: [], items: [] })
    })
    const waitWrites = async count => {
      await page.waitForFunction(() => document.querySelector('.mobile-task-auto-save-status')?.textContent?.includes('已保存'))
      assert.equal(writes.length, count)
    }
    await page.goto(`${origin}/tests/pendingRegistrationDetail.fixture.html${dark ? '?dark' : ''}`)
    const result = page.locator('label').filter({ hasText: /^核查结果/ }).getByRole('combobox')
    const selectResult = async name => { await result.click(); await page.locator('.ant-select-dropdown:visible .ant-select-item-option-content').getByText(name, { exact: true }).click() }
    await selectResult('待登记')
    await waitWrites(1)
    await page.waitForFunction(() => document.querySelector('#registration-match-status')?.textContent?.includes('唯一候选'))
    const address = page.locator('.mobile-task-registration-controls textarea')
    assert.equal(await address.inputValue(), '自填测试地址')
    assert.equal(writes[0].registration_pending_address, '自填测试地址')
    assert.equal(writes[0].changes.核查结果, '待登记')
    assert.equal(writes[0].registration_property_id, undefined)
    assert.ok(searches.length > 0)
    await address.fill('任意格式，无标准地址')
    await address.dispatchEvent('compositionstart')
    await page.waitForTimeout(800)
    assert.equal(writes.length, 1)
    await address.dispatchEvent('compositionend')
    await address.blur()
    await waitWrites(2)
    assert.equal(writes[1].registration_pending_address, '任意格式，无标准地址')
    failSearch = true
    const optional = page.getByRole('combobox', { name: '关联辖区档案房屋（可选）' })
    await optional.fill('搜索失败线索')
    await page.waitForFunction(() => document.querySelector('#registration-match-status')?.textContent?.includes('搜索暂时失败'))
    await address.fill('搜索不可用仍保存')
    await address.blur()
    await waitWrites(3)
    assert.equal(writes[2].registration_pending_address, '搜索不可用仍保存')
    failSearch = false
    await optional.fill('合成')
    await page.locator('.ant-select-dropdown:visible').getByText(property.natural_address, { exact: true }).click()
    await waitWrites(4)
    assert.equal(writes[3].registration_property_id, 23)
    assert.equal(writes[3].registration_property_version, 2)
    assert.equal(writes[3].changes.现住址, property.natural_address)
    assert.equal(writes[3].registration_pending_address, undefined)
    // An older property response must not reattach it to a newer free-address draft.
    delay = 600
    await page.locator('label').filter({ hasText: /^备注/ }).locator('textarea').fill('延迟请求')
    await page.locator('label').filter({ hasText: /^备注/ }).locator('textarea').blur()
    await page.waitForTimeout(100)
    await address.fill('请求期间的自由地址')
    await page.waitForTimeout(800)
    assert.equal(await address.inputValue(), '请求期间的自由地址')
    delay = 0
    await address.blur()
    await waitWrites(6)
    assert.equal(writes[5].registration_property_id, undefined)
    assert.equal(writes[5].registration_pending_address, '请求期间的自由地址')
    failSave = true
    await address.fill('失败保留地址')
    await address.blur()
    await page.getByRole('button', { name: '重试保存' }).waitFor()
    assert.equal(await address.inputValue(), '失败保留地址')
    await page.getByRole('button', { name: '重试保存' }).click()
    await waitWrites(8)
    await address.fill('')
    await address.blur()
    await page.getByText('请填写现住址后自动保存').waitFor()
    await page.waitForTimeout(200)
    assert.equal(writes.length, 8)
    await address.fill('最终自由地址')
    await address.blur()
    await waitWrites(9)
    await page.getByText('待登记未关联房屋', { exact: true }).waitFor()
    const boxes = await page.locator('.mobile-task-registration-controls').evaluate(element => [...element.children].map(child => {
      const box = child.getBoundingClientRect(); return { top: box.top, bottom: box.bottom }
    }))
    for (let index = 1; index < boxes.length; index++) assert.ok(boxes[index].top - boxes[index - 1].bottom >= 7)
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
    mkdirSync('artifacts/pending-registration', { recursive: true })
    await address.scrollIntoViewIfNeeded()
    await page.screenshot({ path: `artifacts/pending-registration/${width}-${dark ? 'dark' : 'light'}.png`, fullPage: true })
    await selectResult('无需登记')
    await waitWrites(10)
    assert.equal(writes[9].registration_pending_address, undefined)
    assert.equal(writes[9].registration_property_id, undefined)
    assert.deepEqual(errors, [])
    console.log(JSON.stringify({ width, dark, writes: writes.length, result: 'passed' }))
    await page.close()
  }
} finally {
  await browser?.close()
  await server.close()
}
