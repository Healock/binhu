import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  accessibleNavigationGroups,
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
    false,
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
    'visit.import',
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
