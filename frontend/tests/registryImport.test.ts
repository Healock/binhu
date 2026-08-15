import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('户号表与确认接口保留长请求超时，告知书改为后台任务', () => {
  const apiSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(apiSource, /api\.post\('\/registry\/imports\/households\/preview', form, \{[\s\S]*?timeout: 300_000/)
  assert.match(apiSource, /api\.post\(`\/registry\/imports\/households\/\$\{batchId\}\/confirm`, \{\}, \{[\s\S]*?timeout: 300_000/)
  assert.match(apiSource, /api\.post\(`\/registry\/imports\/certificates\/\$\{batchId\}\/confirm`, \{\}, \{[\s\S]*?timeout: 300_000/)
  assert.match(apiSource, /api\.post\('\/registry\/imports\/certificates\/source-runs', \{\}/)
  assert.match(apiSource, /api\.get\('\/registry\/imports\/certificates\/source-runs\/latest'\)/)
  assert.match(apiSource, /api\.get\(`\/registry\/imports\/certificates\/source-runs\/\$\{runId\}`\)/)
  assert.match(apiSource, /source-runs\/\$\{runId\}\/retry/)
})

test('辖区档案页面显示告知书后台进度并允许断点继续', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/RegistryManagement.tsx', import.meta.url),
    'utf8',
  )

  assert.match(pageSource, /reason\?\.response\?\.status === 413/)
  assert.match(pageSource, /户号表超过服务器当前上传限制/)
  assert.match(pageSource, /已保存至第 \{certificateRun\.current_page\} 页/)
  assert.match(pageSource, /继续读取/)
  assert.match(pageSource, /重新读取/)
  assert.match(pageSource, /可以离开本页面，任务会在服务器继续执行/)
  assert.doesNotMatch(pageSource, /告知书读取超时/)
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
