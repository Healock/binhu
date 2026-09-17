import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { exportVenueVisitsZip, getVenueVisitPhotoUrl, resolveVenueCodeQrImageUrl } from '../src/api/client.ts'
import { readVenueErrorPayload, venueRegistrationErrorMessage } from '../src/utils/venueRegistration.ts'

test('场所登记按状态和内容类型安全解析错误响应', async () => {
  const json = new Response(JSON.stringify({ detail: '业务校验失败' }), { status: 422, headers: { 'content-type': 'application/json; charset=utf-8' } })
  assert.deepEqual(await readVenueErrorPayload(json), { detail: '业务校验失败' })
  const html413 = new Response('<html><h1>413 Request Entity Too Large</h1></html>', { status: 413, headers: { 'content-type': 'text/html' } })
  assert.equal(await readVenueErrorPayload(html413), null)
  assert.equal(venueRegistrationErrorMessage(413, null), '照片或上传请求超过网关限制，请压缩照片后重试')
  for (const status of [502, 503, 504]) {
    assert.equal(venueRegistrationErrorMessage(status, null), '场所登记服务暂时不可用，请稍后重试')
  }
  assert.equal(venueRegistrationErrorMessage(404, null), '登记接口不存在或二维码入口与当前服务版本不一致，请重新扫码或联系管理员')
  assert.equal(venueRegistrationErrorMessage(422, { detail: '照片内容无效' }), '照片内容无效')
  const html = venueRegistrationErrorMessage(500, null)
  assert.equal(html, '服务器返回了无法识别的错误页面，请稍后重试')
  assert.doesNotMatch(html, /<html|token|身份证|手机号|地址/)
})

test('场所登记照片使用受认证的访问路径', () => {
  assert.equal(getVenueVisitPhotoUrl(42), '/venue-visits/42/photo')
})

test('场所码图片地址在未配置远程 API 时保持同源', () => {
  assert.equal(
    resolveVenueCodeQrImageUrl('/api/venue-codes/7/qrcode?format=png'),
    '/api/venue-codes/7/qrcode?format=png',
  )
})

test('场所码图片地址在桌面客户端解析到远程 API', () => {
  assert.equal(
    resolveVenueCodeQrImageUrl(
      '/api/venue-codes/7/qrcode?format=png',
      'https://api.example.test/api',
    ),
    'https://api.example.test/api/venue-codes/7/qrcode?format=png',
  )
})

test('场所二维码通过受认证 Blob 加载并允许 Tauri 显示 object URL', () => {
  const page = readFileSync(new URL('../src/pages/VenueCodeManagement.tsx', import.meta.url), 'utf8')
  const image = readFileSync(new URL('../src/components/AuthenticatedImage.tsx', import.meta.url), 'utf8')
  const tauri = JSON.parse(readFileSync(new URL('../../desktop/apps/win10-tauri/src-tauri/tauri.conf.json', import.meta.url), 'utf8'))

  assert.match(page, /<AuthenticatedImage alt="场所二维码"/)
  assert.match(page, /loading=\{qrLoadingId === row\.id\}/)
  assert.match(page, /message\.error\(apiErrorMessage\(reason, '二维码读取失败，请稍后重试'\)\)/)
  assert.match(image, /useAuthenticatedImageUrl/)
  assert.match(tauri.app.security.csp, /img-src[^;]*blob:/)
})

test('场所管理提供带二次确认的软删除入口', () => {
  const page = readFileSync(new URL('../src/pages/VenueCodeManagement.tsx', import.meta.url), 'utf8')
  const client = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8')

  assert.match(page, /title="移除这个场所？"/)
  assert.match(page, /既有登记记录仍按原期限保留/)
  assert.match(page, /await deleteVenueCode\(row\.id\)/)
  assert.match(client, /api\.delete\(`\/venue-codes\/\$\{id\}`\)/)
})

test('场所登记列表提供照片查看入口', () => {
  const page = readFileSync(new URL('../src/pages/VenueCodeManagement.tsx', import.meta.url), 'utf8')
  assert.match(page, /getVenueVisitPhotoUrl\(row\.id\)/)
  assert.match(page, /查看照片/)
  assert.match(page, /AuthenticatedImage/)
})

test('场所登记管理支持删除确认且移除旧版单独 XLSX 导出入口', () => {
  const page = readFileSync(new URL('../src/pages/VenueCodeManagement.tsx', import.meta.url), 'utf8')
  const client = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8')
  assert.match(page, /删除这条登记记录？/)
  assert.match(page, /deleteVenueVisit\(row\.id\)/)
  assert.doesNotMatch(page, /导出登记记录/)
  assert.match(client, /api\.delete\(`\/venue-visits\/\$\{id\}`\)/)
})

test('场所登记支持按条件查询并导出包含照片的 ZIP', () => {
  const page = readFileSync(new URL('../src/pages/VenueCodeManagement.tsx', import.meta.url), 'utf8')
  const client = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8')
  assert.match(page, /姓名、身份证号、手机号、地址/)
  assert.match(page, /DatePicker.RangePicker/)
  assert.match(page, /exportVenueVisitsZip\(visitFilters\)/)
  assert.match(page, /导出查询结果（ZIP）/)
  assert.match(client, /api.get\('\/venue-visits\/export-zip'/)
  assert.equal(typeof exportVenueVisitsZip, 'function')
})

test('遗留本地登记组件也把照片作为必填并防止空文件崩溃', () => {
  const page = readFileSync(new URL('../src/pages/VenueCodeManagement.tsx', import.meta.url), 'utf8')
  assert.match(page, /请选择照片/)
  assert.match(page, /!values\.photo\?\.file/)
})

test('场所登记提交不会对错误 HTML 调用无保护的 response.json', () => {
  const page = readFileSync(new URL('../src/pages/VenueCodeManagement.tsx', import.meta.url), 'utf8')
  const submitBlock = page.split("resolveRuntimeApiUrl('/api/public/venue-visits')", 2)[1]?.split('setDone(true)', 1)[0] || ''
  assert.doesNotMatch(submitBlock, /response\.json\(\)/)
  assert.match(submitBlock, /readVenueErrorPayload\(response\)/)
})

test('二维码管理提供饮酒报备创建、查询、双签详情和单份 PDF', () => {
  const page = readFileSync(new URL('../src/pages/VenueCodeManagement.tsx', import.meta.url), 'utf8')
  const client = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8')
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
  const navigation = readFileSync(new URL('../src/navigation/mobileNavigation.ts', import.meta.url), 'utf8')
  assert.match(page, /title="二维码管理"/)
  assert.match(page, /场所登记码/)
  assert.match(page, /饮酒报备码/)
  assert.match(page, /!drinkingForm\?\.exists/)
  assert.match(page, /SignaturePreview/)
  assert.match(page, /导出 A4 PDF/)
  assert.match(page, /formatUTCTime\(value, timezone\)/)
  assert.match(page, /systemTimezone/)
  assert.match(client, /api\.get\('\/drinking-reports'/)
  assert.match(client, /drinking-reports\/\$\{id\}\/pdf/)
  assert.match(app, /path="\/qr-codes"/)
  assert.match(app, /path="\/venue-codes" element=\{<Navigate to="\/qr-codes" replace/)
  assert.match(navigation, /label: '二维码管理'/)
})

test('登记记录支持按场所筛选，饮酒报备提供云端拉取刷新', () => {
  const page = readFileSync(new URL('../src/pages/VenueCodeManagement.tsx', import.meta.url), 'utf8')
  const client = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8')
  assert.match(page, /name="venue_id" label="场所"/)
  assert.match(page, /pullVenueCloudNow\(\)/)
  assert.match(page, /已拉取 .*饮酒报备并入库|当前没有可处理的云端登记/)
  assert.match(client, /api\.post\('\/venue-cloud\/pull'/)
})
