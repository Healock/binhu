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

test('数据上传中心移除自购自住名单入口并拆分文件与外部读取', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/DataUploadCenter.tsx', import.meta.url),
    'utf8',
  )

  assert.doesNotMatch(pageSource, /自购自住人员|自购自住名单|importModelThreeSelfOwnedRoster/)
  assert.match(pageSource, /label: '上传文件'/)
  assert.match(pageSource, /label: '读取外部'/)
  assert.match(pageSource, /ExternalAcquisitionScheduleControls/)
  assert.match(pageSource, /canManageResidence && <Panel title="自动任务编排"/)
  assert.doesNotMatch(pageSource, /canManageSchedules/)
})

test('外部来源读取入口集中到数据上传中心', () => {
  const registry = readFileSync(new URL('../src/pages/RegistryManagement.tsx', import.meta.url), 'utf8')
  const visit = readFileSync(new URL('../src/pages/VisitSummary.tsx', import.meta.url), 'utf8')
  const code = readFileSync(new URL('../src/pages/CodeSummary.tsx', import.meta.url), 'utf8')
  const venue = readFileSync(new URL('../src/pages/VenueCodeManagement.tsx', import.meta.url), 'utf8')
  const settings = readFileSync(new URL('../src/pages/SystemSettings.tsx', import.meta.url), 'utf8')
  const dashboard = readFileSync(new URL('../src/pages/Dashboard.tsx', import.meta.url), 'utf8')

  assert.doesNotMatch(registry, /label: '数据导入'/)
  assert.match(registry, /前往数据上传中心/)
  assert.doesNotMatch(visit, /VisitSourcePanel/)
  assert.doesNotMatch(code, /fetchCodeSummaries\(/)
  assert.match(code, /前往数据上传中心读取/)
  assert.doesNotMatch(venue, /pullVenueCloudNow\(/)
  assert.doesNotMatch(settings, /每日反馈扫描|自动查询开关|重新查询全部任务/)
  assert.doesNotMatch(dashboard, /配置外部监控|panel=txdocs/)
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
