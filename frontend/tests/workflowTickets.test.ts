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
  assert.match(apiSource, /api\.post\('\/workflow\/photo-requests\/pending\/search', payload, activeRequest\)/)
  assert.match(apiSource, /\/workflow\/photo-requests\/batch-claim/)
  assert.match(apiSource, /api\.post\('\/workflow\/photo-requests\/pending\/export', payload/)
  assert.doesNotMatch(apiSource, /api\.get\('\/workflow\/photo-requests/)
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
