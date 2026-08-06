import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const read = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8')

test('全端首页进入岗位仪表盘且在线汇总迁移到独立路由', () => {
  const app = read('../src/App.tsx')
  const configurator = read('../src/components/DockConfigurator.tsx')
  assert.match(app, /path="\/" element=\{<RoleDashboard \/>\}/)
  assert.match(app, /path="\/summary" element=\{<Dashboard \/>\}/)
  assert.match(app, /function QueryEntry\(\)[\s\S]*shouldUseMobileTaskWorkbench/)
  assert.match(configurator, /locked=\{itemId === 'dashboard'\}/)
})

test('岗位仪表盘只消费后端模块并提供筛选直达', () => {
  const source = read('../src/pages/RoleDashboard.tsx')
  for (const moduleName of [
    'flow_tasks',
    'online_overview',
    'visit_overview',
    'dispatch_overview',
    'management',
  ]) {
    assert.match(source, new RegExp(`data\\.${moduleName}`))
  }
  assert.match(source, /category: 'carryover'/)
  assert.match(source, /status=review/)
  assert.match(source, /今日尚无同步快照/)
})

test('用户管理姓名链接到公开资料并保留返回来源', () => {
  const users = read('../src/pages/UserManagement.tsx')
  const profile = read('../src/pages/PublicProfile.tsx')
  assert.match(users, /user\.member\?\.name \|\| value/)
  assert.match(users, /to=\{`\/people\/\$\{user\.id\}`\}/)
  assert.match(users, /returnTo: '\/users'/)
  assert.match(profile, /returnTo = returnState\?\.returnTo \|\| '\/'/)
})

test('任务与汇总页面从 URL 恢复并同步筛选', () => {
  const mobileTasks = read('../src/pages/MobileTaskList.tsx')
  const dispatch = read('../src/pages/PoliceDispatchWorkbench.tsx')
  const summary = read('../src/pages/Dashboard.tsx')
  const visits = read('../src/pages/VisitSummary.tsx')
  assert.match(mobileTasks, /searchParams\.get\('status'\)/)
  assert.match(mobileTasks, /searchParams\.get\('review_stage'\)/)
  assert.match(dispatch, /searchParams\.get\('category'\)/)
  assert.match(summary, /searchParams\.get\('category'\)/)
  assert.match(visits, /searchParams\.get\('start'\)/)
})

test('启用分页数量切换的客户端表格不再写死受控页大小', () => {
  const operations = read('../src/pages/OperationsCenter.tsx')
  const addresses = read('../src/pages/PoliceAddressManagement.tsx')
  assert.doesNotMatch(operations, /pagination=\{\{ pageSize: 20, showSizeChanger: true \}\}/)
  assert.match(operations, /defaultPageSize: 20, showSizeChanger: true/)
  assert.match(addresses, /defaultPageSize: 20, showSizeChanger: true/)
})
