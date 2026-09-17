import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { exportVenueVisitsZip, getVenueVisitPhotoUrl, resolveVenueCodeQrImageUrl } from '../src/api/client.ts'

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
  assert.match(page, /饮酒报备记录已刷新|云端暂无待处理登记/)
  assert.match(client, /api\.post\('\/venue-cloud\/pull'/)
})
