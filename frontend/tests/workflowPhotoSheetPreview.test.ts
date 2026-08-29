import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const configPage = readFileSync(new URL('../src/pages/WorkflowConfig.tsx', import.meta.url), 'utf8')
const apiClient = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8')

test('流程配置不再暴露腾讯调照片名单配置和同步入口', () => {
  for (const text of [
    'photoSheetConfig',
    '/workflow/photo-sheet',
    '腾讯表格地址',
    '待写回队列',
  ]) {
    assert.doesNotMatch(configPage, new RegExp(text))
    assert.doesNotMatch(apiClient, new RegExp(text))
  }
})

test('流程配置仍保留工单类型和版本管理', () => {
  assert.match(configPage, /workflowApi\.types\(\)/)
  assert.match(configPage, /workflowApi\.versions/)
  assert.match(configPage, /新增工单类型/)
  assert.match(configPage, /发布版本/)
})

test('客户端不再提供腾讯调照片名单 API', () => {
  assert.doesNotMatch(apiClient, /photoSheetRuns|photoSheetIssues|retryPhotoSheetOutbox/)
})
