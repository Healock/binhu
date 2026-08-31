import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('基础管控照片工作台支持只读表格、批量领取和 XLSX 导出', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/WorkflowTickets.tsx', import.meta.url),
    'utf8',
  )
  const apiSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(pageSource, /photo_pending/)
  assert.match(pageSource, /领取全部待领取/)
  assert.match(pageSource, /batchClaimPhotoRequests/)
  assert.match(pageSource, /navigate\('\/data-upload'\)/)
  assert.match(pageSource, /导出 XLSX/)
  assert.match(pageSource, /getCheckboxProps: row => \(\{ disabled: row\.status !== 'queued' \}\)/)
  assert.doesNotMatch(pageSource, /QuerySpreadsheet/)
  assert.match(apiSource, /api\.post\([\s\S]*'\/workflow\/photo-requests\/pending\/search'[\s\S]*options\.passive \? passiveRequest : activeRequest/)
  assert.match(apiSource, /\/workflow\/photo-requests\/batch-claim/)
  assert.match(apiSource, /api\.post\('\/workflow\/photo-requests\/pending\/export', payload/)
  assert.doesNotMatch(apiSource, /api\.get\('\/workflow\/photo-requests/)
})

test('照片批次上传使用长超时并明确提示网关大小限制', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/DataUploadCenter.tsx', import.meta.url),
    'utf8',
  )
  const apiSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(apiSource, /api\.post\('\/workflow\/photo-imports\/preview', form, \{[\s\S]*timeout: 300000/)
  assert.match(apiSource, /api\.post\(`\/workflow\/photo-imports\/\$\{batchId\}\/confirm`, \{\}, \{[\s\S]*timeout: 300000/)
  assert.match(pageSource, /error\?\.response\?\.status === 413/)
  assert.match(pageSource, /照片 ZIP 超过服务器上传限制/)
  assert.match(pageSource, /照片 ZIP 上传或解析超时/)
})

test('自购自住名单上传遵守后端大小限制并明确提示网关拒绝', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/DataUploadCenter.tsx', import.meta.url),
    'utf8',
  )

  assert.match(pageSource, /MAX_SELF_OWNED_ZIP_BYTES = 100 \* 1024 \* 1024/)
  assert.match(pageSource, /file\.size > MAX_SELF_OWNED_ZIP_BYTES/)
  assert.match(pageSource, /自购自住名单 ZIP 不能超过 100MB/)
  assert.match(pageSource, /error\?\.response\?\.status === 413/)
  assert.match(pageSource, /自购自住名单 ZIP 超过服务器上传限制/)
  assert.match(pageSource, /error\?\.code === 'ECONNABORTED'/)
  assert.match(pageSource, /自购自住名单上传或处理超时/)
})

test('历史照片批次修复必须先预览再明确确认', () => {
  const apiSource = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8')
  const pageSource = readFileSync(new URL('../src/pages/DataUploadCenter.tsx', import.meta.url), 'utf8')

  assert.match(apiSource, /photo-imports\/\$\{batchId\}\/reconcile-preview/)
  assert.match(apiSource, /photo-imports\/\$\{batchId\}\/reconcile`, \{ confirm: true \}/)
  assert.match(pageSource, /核对遗漏工单/)
  assert.match(pageSource, /确认修复遗漏照片工单/)
  assert.match(pageSource, /eligible_tickets/)
})

test('调照片使用独立任务页面，通用工单中心不再混入照片待办标签', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/WorkflowTickets.tsx', import.meta.url),
    'utf8',
  )
  const appSource = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')

  assert.match(pageSource, /mode = 'tickets'/)
  assert.match(pageSource, /photoOnly \? '调照片' : '工单中心'/)
  assert.doesNotMatch(pageSource, /key: 'photo_pending', label: '未调照片'/)
  assert.match(appSource, /path="\/photo-tasks"/)
  assert.match(appSource, /<WorkflowTickets mode="photo" \/>/)
})

test('管理员和超级管理员可以把待补充工单恢复为待领取', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/WorkflowTickets.tsx', import.meta.url),
    'utf8',
  )
  const apiSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(pageSource, /\['admin', 'super_admin'\]/)
  assert.match(pageSource, /detail\.status === 'pending_requester'/)
  assert.match(pageSource, /恢复待领取/)
  assert.match(pageSource, /restoreQueued\(detail\.id/)
  assert.match(apiSource, /api\.post\(`\/workflow\/tickets\/\$\{id\}\/restore-queued`, payload\)/)
})

test('工单附件查看与管理权限分离', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/WorkflowTickets.tsx', import.meta.url),
    'utf8',
  )

  assert.match(pageSource, /const canViewAttachments = permissions\.has\('workflow\.attachment\.view'\)/)
  assert.match(pageSource, /const canManageAttachments = canHandle/)
  assert.match(pageSource, /\{canManageAttachments && \(/)
  assert.match(pageSource, /\{canViewAttachments \? \(/)
  assert.match(pageSource, /\.\.\.\(canManageAttachments \? \[/)
})
