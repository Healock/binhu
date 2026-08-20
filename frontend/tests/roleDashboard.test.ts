import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { formatDashboardIdentityContext } from '../src/utils/dashboardIdentity.ts'

const read = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8')

test('全端首页进入仪表盘且在线汇总迁移到独立路由', () => {
  const app = read('../src/App.tsx')
  const configurator = read('../src/components/DockConfigurator.tsx')
  assert.match(app, /path="\/" element=\{<RoleDashboard \/>\}/)
  assert.match(app, /path="\/summary" element=\{<Dashboard \/>\}/)
  assert.match(app, /function QueryEntry\(\)[\s\S]*shouldUseMobileTaskWorkbench/)
  assert.match(configurator, /locked=\{itemId === 'dashboard'\}/)
})

test('仪表盘只消费后端模块并提供筛选直达', () => {
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
  assert.match(source, /buildUrl\('\/tasks',[\s\S]*status,/)
  assert.doesNotMatch(source, /label="最终完成"/)
  assert.match(source, /label="已完成"/)
  assert.match(source, /MonoRoundedStackedBarChart/)
  assert.match(source, /overview\.community_breakdown\.map/)
  assert.doesNotMatch(source, /community_breakdown\.slice/)
  const chart = read('../src/components/charts/MonoRoundedStackedBarChart.tsx')
  assert.match(chart, /from 'recharts'/)
  assert.match(chart, /layout="vertical"/)
  assert.match(chart, /dataKey="completed"/)
  assert.match(chart, /dataKey="unable"/)
  assert.match(chart, /dataKey="pending"/)
  const monoCharts = read('../src/components/charts/MonoBusinessCharts.tsx')
  assert.match(source, /MonoKpiSparkline/)
  assert.match(source, /MonoBulletChart/)
  assert.match(source, /MonoDonutChart/)
  assert.match(monoCharts, /export function MonoKpiSparkline/)
  assert.match(monoCharts, /export function MonoBulletChart/)
  assert.match(monoCharts, /export function MonoTrendChart/)
  assert.match(monoCharts, /export function MonoDonutChart/)
  assert.match(monoCharts, /export function MonoWaterfallChart/)
  assert.match(source, /actual=\{Number\(week\.completed_tasks \|\| 0\)\}/)
  assert.match(source, /target=\{Number\(week\.total_tasks \|\| 0\)\}/)
  assert.match(source, /label: '待审核'/)
  assert.match(source, /label: '待发布'/)
  assert.match(source, /label: '已发布'/)
})

test('运维和照片批次图表使用真实序列且保留明细表', () => {
  const operations = read('../src/pages/OperationsCenter.tsx')
  const uploads = read('../src/pages/DataUploadCenter.tsx')
  const calendar = read('../src/components/ContributionCalendar.tsx')

  assert.match(operations, /requestUsage\.daily\.map/)
  assert.match(operations, /data\.sync_daily_counts\.map/)
  assert.match(operations, /<MonoTrendChart/)
  assert.match(operations, /<Table[\s\S]*dataSource=\{requestUsage\?\.daily \|\| \[\]\}/)
  assert.match(operations, /<Table[\s\S]*dataSource=\{data\?\.sync_daily_counts \|\| \[\]\}/)
  assert.match(uploads, /<MonoWaterfallChart/)
  for (const field of ['matched_files', 'unmatched_files', 'conflict_files', 'duplicate_files', 'failed_files']) {
    assert.match(uploads, new RegExp(`photoBatch\\.${field}`))
  }
  assert.match(calendar, /rectProps=\{\{ rx: 3, ry: 3 \}\}/)
})

test('登录设备和修改密码统一进入账号与安全设置', () => {
  const app = read('../src/App.tsx')
  const settings = read('../src/components/SettingsLayout.tsx')
  const security = read('../src/pages/AccountSecuritySettings.tsx')
  const personalization = read('../src/pages/PersonalizationSettings.tsx')
  const profile = read('../src/pages/Profile.tsx')
  const layout = read('../src/components/Layout.tsx')

  assert.match(app, /path="account-security" element=\{<AccountSecuritySettings \/>\}/)
  assert.match(settings, /\/settings\/account-security'[\s\S]*账号与安全/)
  assert.match(security, /title="修改密码"|>修改密码</)
  assert.match(security, />登录设备</)
  assert.doesNotMatch(personalization, /登录设备/)
  assert.doesNotMatch(profile, /title="修改密码"/)
  assert.match(layout, /navigate\('\/settings\/account-security'\)/)
})

test('平安码汇总允许最近获取记录为空', () => {
  const source = read('../src/pages/CodeSummary.tsx')
  assert.match(source, /report\?\.latest_run\?\.unclassified_count \?\? 0/)
  assert.doesNotMatch(source, /locationReport\?\.unclassified_count \?\? report\.latest_run\.unclassified_count/)
})

test('实际工作次数连续点击十次打开无路由隐藏工作区', () => {
  const dashboard = read('../src/pages/RoleDashboard.tsx')
  const styles = read('../src/index.css')
  const overlay = read('../src/components/HiddenWorkspaceOverlay.tsx')
  const app = read('../src/App.tsx')
  assert.match(dashboard, /onClick,\s*onHintClick,/)
  assert.match(dashboard, /onHintClick=\{handleSecretClick\}/)
  assert.match(dashboard, /<span\s+className="role-dashboard-metric__hint role-dashboard-metric__hint--secret"/)
  assert.doesNotMatch(dashboard, /<button[\s\S]*role-dashboard-metric__hint--secret/)
  assert.match(styles, /\.role-dashboard-metric__label,\s*\.role-dashboard-metric__hint\s*\{[\s\S]*font-size: 12px;/)
  assert.match(styles, /\.role-dashboard-metric__hint--secret\s*\{[\s\S]*cursor: inherit;/)
  assert.doesNotMatch(styles, /\.role-dashboard-metric__hint--secret\s*\{[\s\S]*?font:\s*inherit;/)
  assert.match(dashboard, /clicks\.count >= 10/)
  assert.match(dashboard, /now - clicks\.lastAt > 2500/)
  assert.match(dashboard, /<HiddenWorkspaceOverlay open=\{hiddenWorkspaceOpen\}/)
  assert.match(overlay, /createPortal\([\s\S]*document\.body/)
  assert.match(overlay, /event\.key === 'Escape'/)
  assert.doesNotMatch(app, /path="\/hidden/)
})

test('仪表盘使用双列瀑布流并在手机端回到单列', () => {
  const styles = read('../src/index.css')
  assert.match(styles, /\.role-dashboard-sections\s*\{[\s\S]*columns: 2;/)
  assert.match(styles, /\.role-dashboard-sections > \*\s*\{[\s\S]*break-inside: avoid;/)
  assert.match(styles, /\.role-dashboard-sections\s*\{\s*columns: 1;/)
})

test('手机 Dock 不再单列首页且顶部品牌返回仪表盘', () => {
  const dock = read('../src/components/MobileDock.tsx')
  const layout = read('../src/components/Layout.tsx')
  assert.doesNotMatch(dock, /<span>首页<\/span>/)
  assert.match(layout, /aria-label="返回仪表盘"[\s\S]*navigate\('\/'\)/)
})

test('社区部门与职责社区相同时只展示一次', () => {
  assert.equal(
    formatDashboardIdentityContext(['冬梅'], '所属社区：冬梅', ['冬梅']),
    '所属社区：冬梅',
  )
  assert.equal(
    formatDashboardIdentityContext(['内勤'], '全所', null),
    '内勤 · 全所',
  )
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

test('走访汇总无日期参数时默认查询系统当天', () => {
  const visits = read('../src/pages/VisitSummary.tsx')
  assert.match(visits, /formatDateInTimezone\(new Date\(\), systemTimezone\)/)
  assert.match(visits, /const initialRange: \[string, string\] = \[\s*fallbackDate,\s*fallbackDate,\s*\]/)
  assert.doesNotMatch(visits, /nextCoverage\.start_date \|\| fallbackDate/)
})

test('启用分页数量切换的客户端表格不再写死受控页大小', () => {
  const operations = read('../src/pages/OperationsCenter.tsx')
  const addresses = read('../src/pages/PoliceAddressManagement.tsx')
  assert.doesNotMatch(operations, /pagination=\{\{ pageSize: 20, showSizeChanger: true \}\}/)
  assert.match(operations, /defaultPageSize: 20, showSizeChanger: true/)
  assert.match(addresses, /defaultPageSize: 20, showSizeChanger: true/)
})

test('小区管理支持本社区锁定、真实删除和范围导出', () => {
  const addresses = read('../src/pages/PoliceAddressManagement.tsx')
  const client = read('../src/api/client.ts')
  assert.match(addresses, /communityLocked/)
  assert.match(addresses, /disabled=\{communityLocked\}/)
  assert.match(addresses, /deletePoliceAddress/)
  assert.match(addresses, /导出 XLSX/)
  assert.match(addresses, /<PageHeader[\s\S]*?actions=\{/)
  assert.match(client, /post\('\/police-dispatch\/addresses\/export'/)
  assert.match(client, /responseType: 'blob'/)
})
