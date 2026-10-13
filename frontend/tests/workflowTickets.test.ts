import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('照片工作台已从通用工单中心和客户端 API 退役', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/WorkflowTickets.tsx', import.meta.url),
    'utf8',
  )
  const apiSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.doesNotMatch(pageSource, /photo_pending|领取全部待领取|batchClaimPhotoRequests|导出 XLSX/)
  assert.doesNotMatch(pageSource, /QuerySpreadsheet/)
  assert.doesNotMatch(apiSource, /\/workflow\/photo-requests|\/workflow\/photo-imports/)
})

test('数据上传中心不再提供照片批次上传', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/DataUploadCenter.tsx', import.meta.url),
    'utf8',
  )
  const apiSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.doesNotMatch(apiSource, /\/workflow\/photo-imports/)
  assert.doesNotMatch(pageSource, /照片 ZIP|photo-imports/)
})

test('自购自住名单上传遵守后端大小限制并明确提示网关拒绝', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/DataUploadCenter.tsx', import.meta.url),
    'utf8',
  )

  assert.match(pageSource, /MAX_SELF_OWNED_ZIP_BYTES = 100 \* 1024 \* 1024/)
  assert.match(pageSource, /file\.size > MAX_SELF_OWNED_ZIP_BYTES/)
  assert.match(pageSource, /自购自住名单 ZIP 不能超过 100MB/)
  assert.match(pageSource, /const status = error\?\.response\?\.status/)
  assert.match(pageSource, /自购自住名单 ZIP 超过服务器上传限制/)
  assert.match(pageSource, /error\?\.code === 'ECONNABORTED'/)
  assert.match(pageSource, /自购自住名单上传或处理超时/)
})

test('历史照片批次修复入口已退役', () => {
  const apiSource = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8')
  const pageSource = readFileSync(new URL('../src/pages/DataUploadCenter.tsx', import.meta.url), 'utf8')

  assert.doesNotMatch(apiSource, /photo-imports|eligible_tickets/)
  assert.doesNotMatch(pageSource, /核对遗漏工单|确认修复遗漏照片工单/)
})

test('调照片页面和入口已移除', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/WorkflowTickets.tsx', import.meta.url),
    'utf8',
  )
  const appSource = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')

  assert.doesNotMatch(pageSource, /mode = 'photo'|photoOnly|photo_pending/)
  assert.doesNotMatch(appSource, /\/photo-tasks|WorkflowTickets mode="photo"/)
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
