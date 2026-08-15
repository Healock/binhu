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

test('房屋档案和问题核查使用正文搜索并提供完整筛选', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/RegistryManagement.tsx', import.meta.url),
    'utf8',
  )
  const apiSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(apiSource, /api\.post\('\/registry\/properties\/search', params/)
  assert.match(apiSource, /api\.post\('\/registry\/import\/issues\/search', params/)
  assert.match(pageSource, /搜索地址、户号、幢室或住房类型/)
  assert.match(pageSource, /全部社区/)
  assert.match(pageSource, /出租房/)
  assert.match(pageSource, /自购房/)
  assert.match(pageSource, /全部住房类型/)
  assert.match(pageSource, /pagination=\{listPagination\}/)
})

test('问题数据核查说明外部修正原因并显示字段和错误值', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/RegistryManagement.tsx', import.meta.url),
    'utf8',
  )

  assert.match(pageSource, /到居住证系统更新正确内容/)
  assert.match(pageSource, /问题字段与错误值/)
  assert.match(pageSource, /为什么有问题/)
  assert.match(pageSource, /registry-issue-evidence__value/)
  assert.doesNotMatch(pageSource, /标记已核查/)
  assert.doesNotMatch(pageSource, /reviewImportIssue/)
  assert.match(pageSource, /key: 'imports', label: '数据导入'/)
})
