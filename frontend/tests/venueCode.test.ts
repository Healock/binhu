import assert from 'node:assert/strict'
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
