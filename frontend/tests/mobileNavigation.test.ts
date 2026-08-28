import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  accessibleNavigationGroups,
  mobileAccessibleNavigationGroups,
  defaultMobileDockConfig,
  normalizeMobileDockConfig,
  reorderMobileDockGroups,
  reorderMobileDockItems,
} from '../src/navigation/mobileNavigation.ts'

test('普通账号默认 Dock 隐藏超级管理员页面', () => {
  const config = defaultMobileDockConfig('member')

  assert.deepEqual(
    config.groups.map(group => group.id),
    ['workspace', 'system'],
  )
  assert.equal(
    config.groups.some(group => group.items.includes('users')),
    false,
  )
  assert.equal(
    config.groups.some(group => group.items.includes('operations')),
    false,
  )
})

test('超级管理员默认 Dock 使用前四类，设置类仍可配置加入', () => {
  const config = defaultMobileDockConfig('super_admin')
  const accessible = accessibleNavigationGroups('super_admin')

  assert.equal(
    config.groups.some(group => group.items.includes('users')),
    true,
  )
  assert.equal(
    config.groups.some(group => group.items.includes('operations')),
    true,
  )
  assert.equal(
    accessible.some(group => group.items.some(item => item.id === 'operations')),
    true,
  )
  assert.equal(
    config.groups.some(group => group.items.includes('permission_groups')),
    true,
  )
  assert.equal(
    config.groups.some(group => group.items.includes('data_upload')),
    true,
  )
  assert.equal(
    config.groups.some(group => group.items.includes('flow_tasks')),
    true,
  )
})

test('管理员权限组可以配置独立流口任务入口', () => {
  const regular = defaultMobileDockConfig('member', ['online.raw.view'])
  const delegatedAdmin = defaultMobileDockConfig(
    'member',
    ['online.raw.view'],
    ['admin'],
  )

  assert.equal(
    regular.groups.some(group => group.items.includes('flow_tasks')),
    false,
  )
  assert.equal(
    delegatedAdmin.groups.some(group => group.items.includes('flow_tasks')),
    true,
  )
  assert.equal(
    regular.groups.some(group => group.items.includes('online_query')),
    false,
  )
  assert.equal(
    delegatedAdmin.groups.some(group => group.items.includes('online_query')),
    true,
  )
})

test('上级任务岗位在具备查看权限时显示流口任务入口', () => {
  for (const position of ['片长', '基础管控', '中队长', '所队领导']) {
    const config = defaultMobileDockConfig(
      'member',
      ['online.raw.view'],
      [],
      position,
    )
    assert.equal(
      config.groups.some(group => group.items.includes('flow_tasks')),
      true,
      position,
    )
  }
})

test('新权限列表优先于旧角色决定 Dock 页面', () => {
  const config = defaultMobileDockConfig('member', [
    'online.summary.view',
    'police.dispatch.manage',
    'worklog.manage',
  ])

  assert.equal(
    config.groups.some(group => group.items.includes('data_upload')),
    true,
  )
  assert.equal(
    config.groups.some(group => group.items.includes('work_log')),
    true,
  )
  assert.equal(
    config.groups.some(group => group.items.includes('online_query')),
    false,
  )
})

test('电脑端保留汇总导航，手机端移除整个汇总分组', () => {
  const desktop = accessibleNavigationGroups('admin', [
    'online.summary.view',
    'visit.summary.view',
  ])
  const mobile = mobileAccessibleNavigationGroups('admin', [
    'online.summary.view',
    'visit.summary.view',
  ])

  assert.deepEqual(
    desktop.find(group => group.id === 'summaries')?.items.map(item => item.id),
    ['online_summary', 'visit_summary', 'code_summary'],
  )
  assert.equal(mobile.some(group => group.id === 'summaries'), false)
  assert.equal(
    defaultMobileDockConfig('admin', ['online.summary.view', 'visit.summary.view'])
      .groups.some(group => group.id === 'summaries'),
    false,
  )
})

test('下发权限可以单独进入数据上传中心和小区管理', () => {
  const config = defaultMobileDockConfig('member', [
    'police.dispatch.manage',
    'police.address.manage',
  ])

  assert.equal(
    config.groups.some(group => group.items.includes('data_upload')),
    true,
  )
  assert.equal(
    config.groups.some(group => group.items.includes('police_addresses')),
    true,
  )
})

test('组长和组员具备小区权限时可以进入小区管理', () => {
  for (const position of ['组长', '组员']) {
    const config = defaultMobileDockConfig(
      'member',
      ['police.address.manage'],
      ['flow_post', 'community_address_manager'],
      position,
    )
    assert.equal(
      config.groups.some(group => group.items.includes('police_addresses')),
      true,
    )
  }
})

test('数据上传中心只出现在管理员和超级管理员导航中', () => {
  const member = defaultMobileDockConfig('member')
  const admin = defaultMobileDockConfig('admin')

  assert.equal(
    member.groups.some(group => group.items.includes('data_upload')),
    false,
  )
  assert.equal(
    admin.groups.some(group => group.items.includes('data_upload')),
    true,
  )
})

test('工作日志只出现在管理员和超级管理员导航中', () => {
  const member = defaultMobileDockConfig('member')
  const admin = defaultMobileDockConfig('admin')
  const superAdmin = defaultMobileDockConfig('super_admin')

  assert.equal(
    member.groups.some(group => group.items.includes('work_log')),
    false,
  )
  assert.equal(
    admin.groups.some(group => group.items.includes('work_log')),
    true,
  )
  assert.equal(
    superAdmin.groups.some(group => group.items.includes('work_log')),
    true,
  )
})

test('文件生成保留原工作日志导航 ID', () => {
  const source = readFileSync(
    new URL('../src/navigation/mobileNavigation.ts', import.meta.url),
    'utf8',
  )
  assert.match(source, /id: 'work_log'/)
  assert.match(source, /label: '文件生成'/)
  assert.match(source, /shortLabel: '文件生成'/)
})

test('工单中心移入工作台，研判和调照片归入任务处理', () => {
  const groups = accessibleNavigationGroups(
    'member',
    [
      'workflow.ticket.view',
      'workflow.ticket.handle',
      'police.dispatch.manage',
    ],
    [],
    '基础管控',
  )
  const workspace = groups.find(group => group.id === 'workspace')
  const tasks = groups.find(group => group.id === 'tasks')

  assert.equal(workspace?.items.some(item => item.id === 'workflow_tickets'), true)
  assert.equal(tasks?.items.some(item => item.id === 'workflow_tickets'), false)
  assert.equal(tasks?.items.some(item => item.id === 'police_analysis'), true)
  assert.equal(tasks?.items.some(item => item.id === 'photo_tasks'), true)
})

test('侧边栏使用 Tabler 业务图标并通过 currentColor 适配主题', () => {
  const navigation = readFileSync(
    new URL('../src/navigation/mobileNavigation.ts', import.meta.url),
    'utf8',
  )
  const icon = readFileSync(
    new URL('../src/components/NavigationIcon.tsx', import.meta.url),
    'utf8',
  )

  assert.match(icon, /from '@tabler\/icons-react'/)
  assert.doesNotMatch(icon, /from '@ant-design\/icons'/)
  assert.match(icon, /data_query: IconDatabaseSearch/)
  assert.match(icon, /ticket_center: IconTicket/)
  assert.match(icon, /analysis: IconBrain/)
  assert.match(icon, /photo: IconPhotoSearch/)
  assert.match(icon, /registry: IconHomeSearch/)
  assert.match(icon, /permissions: IconShieldLock/)
  assert.match(icon, /size="1em"/)
  assert.match(icon, /stroke=\{1\.8\}/)
  assert.match(navigation, /id: 'police_analysis'[\s\S]*?icon: 'analysis'/)
  assert.match(navigation, /id: 'photo_tasks'[\s\S]*?icon: 'photo'/)
  assert.match(navigation, /id: 'online_summary'[\s\S]*?icon: 'summary'/)
  assert.match(navigation, /id: 'code_summary'[\s\S]*?icon: 'code_summary'/)
  assert.match(navigation, /id: 'communities'[\s\S]*?icon: 'communities'/)
  assert.match(navigation, /id: 'police_addresses'[\s\S]*?icon: 'neighborhoods'/)
  assert.match(navigation, /id: 'registry'[\s\S]*?icon: 'registry'/)
  assert.match(navigation, /id: 'users'[\s\S]*?icon: 'users'/)
  assert.match(navigation, /id: 'permission_groups'[\s\S]*?icon: 'permissions'/)
})

test('调照片独立入口受处理权限和岗位共同限制', () => {
  const regular = accessibleNavigationGroups(
    'member',
    ['workflow.ticket.view', 'workflow.ticket.handle'],
    [],
    '组员',
  )
  const photoHandler = accessibleNavigationGroups(
    'member',
    ['workflow.ticket.view', 'workflow.ticket.handle'],
    [],
    '基础管控',
  )

  assert.equal(regular.some(group => group.items.some(item => item.id === 'photo_tasks')), false)
  assert.equal(photoHandler.some(group => group.items.some(item => item.id === 'photo_tasks')), true)
})

test('旧 Dock 中的工单和下发入口迁移到新的独立页面', () => {
  const config = normalizeMobileDockConfig({
    version: 2,
    groups: [
      { id: 'workspace', items: ['dashboard'] },
      { id: 'tasks', items: ['police_tasks', 'workflow_tickets'] },
    ],
  }, 'member', [
    'police.dispatch.manage',
    'workflow.ticket.view',
    'workflow.ticket.handle',
  ], [], '基础管控')

  assert.deepEqual(
    config.groups.find(group => group.id === 'workspace')?.items,
    ['dashboard', 'workflow_tickets'],
  )
  assert.deepEqual(
    config.groups.find(group => group.id === 'tasks')?.items,
    ['police_tasks', 'police_analysis', 'photo_tasks'],
  )
})

test('读取配置时去重、过滤未知项和无权限页面并保留顺序', () => {
  const config = normalizeMobileDockConfig({
    groups: [
      {
        id: 'resources',
        items: ['communities', 'users', 'communities', 'online_query'],
      },
      {
        id: 'workspace',
        items: ['visit_summary', 'online_summary'],
      },
      {
        id: 'resources',
        items: ['grid_members'],
      },
    ],
  }, 'member')

  assert.deepEqual(config, {
    version: 2,
    groups: [
      {
        id: 'workspace',
        items: ['dashboard'],
      },
      {
        id: 'system',
        items: ['settings'],
      },
    ],
  })
})

test('角色变化后配置全部失效时恢复该角色默认 Dock', () => {
  const config = normalizeMobileDockConfig({
    groups: [
      {
        id: 'resources',
        items: ['users'],
      },
      {
        id: 'system',
        items: ['operations'],
      },
    ],
  }, 'member')

  assert.deepEqual(config, defaultMobileDockConfig('member'))
})

test('读取配置时丢弃空分类并保留其余有效分类', () => {
  const config = normalizeMobileDockConfig({
    groups: [
      { id: 'workspace', items: [] },
      { id: 'resources', items: ['grid_members'] },
      { id: 'system', items: ['settings'] },
    ],
  }, 'member')

  assert.deepEqual(config, {
    version: 2,
    groups: [
      { id: 'workspace', items: ['dashboard'] },
      { id: 'system', items: ['settings'] },
    ],
  })
})

test('拖动分类和页面后按目标位置保存顺序', () => {
  const original = defaultMobileDockConfig('member')
  const movedGroups = reorderMobileDockGroups(
    original,
    'system',
    'workspace',
  )
  assert.deepEqual(
    movedGroups.groups.map(group => group.id),
    ['system', 'workspace'],
  )

  const movedItems = reorderMobileDockItems(
    {
      version: 2,
      groups: [
        { id: 'workspace', items: ['dashboard', 'online_query'] },
        { id: 'system', items: ['settings'] },
      ],
    },
    'workspace',
    'online_query',
    'dashboard',
  )
  assert.deepEqual(
    movedItems.groups.find(group => group.id === 'workspace')?.items,
    ['online_query', 'dashboard'],
  )
  assert.deepEqual(
    original.groups.map(group => group.id),
    ['workspace', 'system'],
  )
})

test('仪表盘固定为 Dock 第一项且旧配置会自动补齐', () => {
  const oldConfig = normalizeMobileDockConfig({
    groups: [{ id: 'system', items: ['settings'] }],
  }, 'member')
  assert.deepEqual(oldConfig.groups[0], {
    id: 'workspace',
    items: ['dashboard'],
  })
  assert.equal(defaultMobileDockConfig('member').groups[0].items[0], 'dashboard')
})

test('平安码管家码汇总页面和导航接入', () => {
  const page = readFileSync(new URL('../src/pages/CodeSummary.tsx', import.meta.url), 'utf8')
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
  const navigation = readFileSync(new URL('../src/navigation/mobileNavigation.ts', import.meta.url), 'utf8')
  const client = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8')

  assert.match(page, /自动获取平安码管家码数据/)
  assert.match(page, /平安码/)
  assert.match(page, /管家码/)
  assert.match(page, /RangePicker/)
  assert.match(app, /path="\/code-summary"/)
  assert.match(navigation, /id: 'code_summary'/)
  assert.match(client, /api\.post\('\/code-summaries\/fetch'/)
  assert.match(client, /api\.post\('\/code-summaries\/search'/)
  assert.match(page, /tables: \[exportTable\(peace, 'peace'\), exportTable\(manager, 'manager'\)\]/)
  assert.match(page, /未分类扫码数/)
  assert.match(page, /新增登记数/)
  assert.match(page, /全链条创建时间和核查结果实时统计/)
  assert.match(page, /缺少有效 comparisonTime/)
  assert.match(client, /visits\/sources\/preview'[\s\S]*timeout:\s*300000/)
  assert.match(page, /invalid_time_count/)
  assert.match(page, /if \(!includeTotal\) return report\.data/)
  assert.match(page, /summaryRows\(report, startDate !== endDate\)/)
  assert.match(page, /位置分类核查/)
  assert.match(page, /仅未分类/)
  assert.match(page, /code\.summary\.manage/)
  assert.match(client, /api\.post\('\/code-summaries\/locations\/search'/)
  assert.match(client, /api\.post\('\/code-summaries\/locations\/classifications'/)
  assert.match(client, /api\.post\('\/code-summaries\/locations\/recompute'/)
})

test('历史手机 Dock 配置中的汇总入口会被清理', () => {
  const config = normalizeMobileDockConfig({
    version: 2,
    groups: [
      { id: 'summaries', items: ['online_summary', 'visit_summary', 'code_summary'] },
      { id: 'workspace', items: ['dashboard', 'online_summary'] },
    ],
  }, 'admin', ['online.summary.view', 'visit.summary.view'])

  assert.equal(config.groups.some(group => group.id === 'summaries'), false)
  assert.equal(config.groups.some(group => group.items.some(item => (
    item === 'online_summary' || item === 'visit_summary' || item === 'code_summary'
  ))), false)
})

test('数据上传中心移除手动走访和星级上传入口', () => {
  const source = readFileSync(new URL('../src/pages/DataUploadCenter.tsx', import.meta.url), 'utf8')

  assert.doesNotMatch(source, /上传走访明细/)
  assert.doesNotMatch(source, /上传星级评定/)
  assert.match(source, /PoliceDispatchPanel/)
  assert.match(source, /照片调取批次/)
})

test('在线人数按钮再次点击会关闭当前弹层', () => {
  const source = readFileSync(
    new URL('../src/components/OnlinePresenceIndicator.tsx', import.meta.url),
    'utf8',
  )

  assert.match(source, /onClick=\{mobile && canViewDetails \? \(\) => setOpen\(current => !current\) : undefined\}/)
  assert.match(source, /<Popover[\s\S]*onOpenChange=\{setOpen\}[\s\S]*trigger="click"/)
  assert.doesNotMatch(source, /onClick=\{\(\) => canViewDetails && setOpen\(true\)\}/)
})

test('在线人数在前台聚焦、网络恢复和心跳周期都会刷新', () => {
  const source = readFileSync(
    new URL('../src/components/OnlinePresenceIndicator.tsx', import.meta.url),
    'utf8',
  )

  assert.match(source, /window\.addEventListener\('focus', onFocus\)/)
  assert.match(source, /window\.addEventListener\('online', onOnline\)/)
  assert.match(source, /window\.addEventListener\('pageshow', onPageShow\)/)
  assert.match(source, /scheduleHeartbeat\(HEARTBEAT_INTERVAL_MS\)/)
  assert.match(source, /if \(open && canViewDetails\) void refreshUsers\(\)/)
  assert.match(source, /void refreshUsers\(true\)/)
})

test('工单流程配置只从设置页进入，不再出现在主侧边栏导航', () => {
  const navigation = readFileSync(
    new URL('../src/navigation/mobileNavigation.ts', import.meta.url),
    'utf8',
  )
  const settings = readFileSync(
    new URL('../src/components/SettingsLayout.tsx', import.meta.url),
    'utf8',
  )

  assert.doesNotMatch(navigation, /id: 'workflow_config'/)
  assert.match(settings, /path: '\/settings\/workflow', label: '工单流程配置'/)
})

test('人员标签不再作为独立导航项，旧 Dock 配置会自动清理', () => {
  const source = readFileSync(
    new URL('../src/navigation/mobileNavigation.ts', import.meta.url),
    'utf8',
  )
  const config = normalizeMobileDockConfig({
    version: 2,
    groups: [{ id: 'resources', items: ['registry', 'watch_people' as any] }],
  }, 'admin', ['registry.property.view', 'registry.watch.view'])

  assert.doesNotMatch(source, /id: 'watch_people'/)
  assert.deepEqual(
    config.groups.find(group => group.id === 'resources')?.items,
    ['registry'],
  )
})
