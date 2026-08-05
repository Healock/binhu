import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  contributionDateLabel,
  contributionDaysForYear,
  contributionLevel,
} from '../src/utils/contributionCalendar.ts'
import { defaultMobileDockConfig } from '../src/navigation/mobileNavigation.ts'

test('贡献强度使用固定工作量区间', () => {
  assert.deepEqual(
    [0, 1, 2, 3, 4, 7, 8, 20].map(contributionLevel),
    [0, 1, 2, 2, 3, 3, 4, 4],
  )
})

test('热力图只接收所选年度日期并保留年度边界', () => {
  assert.deepEqual(contributionDaysForYear([
    { date: '2025-12-31', count: 8 },
    { date: '2026-01-01', count: 1 },
    { date: '2026-12-31', count: 2 },
    { date: '2027-01-01', count: 3 },
    { date: '2026-06-01', count: 0 },
  ], 2026), [
    { date: '2026-01-01', count: 1 },
    { date: '2026-12-31', count: 2 },
  ])
  assert.equal(contributionDateLabel('2026/8/6'), '2026年8月6日')
})

test('所有角色都能在基础资料中配置人员主页入口', () => {
  for (const role of ['member', 'leader', 'admin', 'super_admin'] as const) {
    const config = defaultMobileDockConfig(role)
    assert.equal(
      config.groups.some(group => group.items.includes('people')),
      true,
      role,
    )
  }
})

test('公开个人主页不渲染用户名或敏感字段', () => {
  const source = readFileSync(
    new URL('../src/pages/PublicProfile.tsx', import.meta.url),
    'utf8',
  )
  for (const forbidden of ['username', '身份证号', '手机号', 'permission_groups']) {
    assert.equal(source.includes(forbidden), false, forbidden)
  }
})

test('登录错误提示与按钮使用独立间距容器', () => {
  const source = readFileSync(
    new URL('../src/pages/Login.tsx', import.meta.url),
    'utf8',
  )
  assert.match(source, /<div className="grid gap-3 pt-1">[\s\S]*<Alert[\s\S]*<Button/)
})
