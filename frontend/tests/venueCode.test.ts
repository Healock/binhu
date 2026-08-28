import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { resolveVenueCodeQrImageUrl } from '../src/api/client.ts'

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
  assert.match(image, /useAuthenticatedImageUrl/)
  assert.match(tauri.app.security.csp, /img-src[^;]*blob:/)
})
