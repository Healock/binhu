import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('大批量房屋档案预览与确认使用五分钟请求超时', () => {
  const apiSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(apiSource, /api\.post\('\/registry\/imports\/households\/preview', form, \{[\s\S]*?timeout: 300_000/)
  assert.match(apiSource, /api\.post\(`\/registry\/imports\/households\/\$\{batchId\}\/confirm`, \{\}, \{[\s\S]*?timeout: 300_000/)
  assert.match(apiSource, /api\.post\('\/registry\/imports\/certificates\/source-preview', \{\}, \{[\s\S]*?timeout: 300_000/)
  assert.match(apiSource, /api\.post\(`\/registry\/imports\/certificates\/\$\{batchId\}\/confirm`, \{\}, \{[\s\S]*?timeout: 300_000/)
})

test('辖区档案页面明确区分上传过大和读取超时', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/RegistryManagement.tsx', import.meta.url),
    'utf8',
  )

  assert.match(pageSource, /reason\?\.response\?\.status === 413/)
  assert.match(pageSource, /户号表超过服务器当前上传限制/)
  assert.match(pageSource, /告知书读取超时/)
})
