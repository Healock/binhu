import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const configPage = readFileSync(new URL('../src/pages/WorkflowConfig.tsx', import.meta.url), 'utf8')
const apiClient = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8')

test('photo sheet preview separates blocking issues, warnings, and converted dates', () => {
  assert.match(configPage, /需补充：/)
  assert.match(configPage, /历史格式警告：/)
  assert.match(configPage, /已识别 Excel 日期：/)
  assert.match(configPage, /待办需补充：/)
  assert.match(configPage, /平台首次发现时间作为申请时间/)
  assert.doesNotMatch(configPage, /<div>数据异常：/)
})

test('photo sheet preview API exposes safe aggregate counters only', () => {
  for (const field of [
    'blocking_issue_count',
    'warning_count',
    'identity_empty_count',
    'identity_invalid_count',
    'excel_date_converted_count',
    'request_date_missing_count',
    'request_date_invalid_count',
    'marker_time_invalid_count',
    'pending_blocking_count',
    'pending_warning_count',
  ]) {
    assert.match(apiClient, new RegExp(`${field}: number`))
  }
})
