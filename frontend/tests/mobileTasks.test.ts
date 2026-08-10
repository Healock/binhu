import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  buildMobileTaskChanges,
  mobileTaskEditorFields,
  mobileTaskCanLaunchTelephone,
  mobileTaskPhoneOptions,
  mobileTaskPhoneValue,
  mobileTaskSourceTags,
  mobileTaskSourceDifferences,
  mobileTaskSourceNeedsReview,
  mobileTaskSourceState,
  sortMobileTaskBusinesses,
} from '../src/utils/mobileTasks.ts'
import {
  canAccessFlowTaskWorkbench,
  canBulkAssignMobileTasks,
  isFlowTaskAdmin,
  isFlowTaskElevated,
  isFlowTaskPosition,
  isPoliceDispatchTaskPosition,
  shouldUseMobileTaskWorkbench,
  shouldUsePoliceDispatchWorkbench,
} from '../src/utils/mobileTaskRouting.ts'
import {
  mobileNavigationItemLabel,
  navigationItemById,
  routeIsActive,
} from '../src/navigation/mobileNavigation.ts'

test('组员和组长手机端自动分流，岗位判断也允许桌面任务路由', () => {
  assert.equal(isFlowTaskPosition('组员'), true)
  assert.equal(isFlowTaskPosition('组长'), true)
  assert.equal(shouldUseMobileTaskWorkbench('组员', true), true)
  assert.equal(shouldUseMobileTaskWorkbench('组员', false), false)
  assert.equal(shouldUseMobileTaskWorkbench('基础管控', true), false)
})

test('管理员和超级管理员可以进入流口岗任务工作台', () => {
  assert.equal(isFlowTaskAdmin('admin'), true)
  assert.equal(isFlowTaskAdmin('member', ['admin']), true)
  assert.equal(canAccessFlowTaskWorkbench('', 'super_admin'), true)
  assert.equal(canAccessFlowTaskWorkbench('社区民警', 'member', []), false)
})

test('组长及上级任务岗位可以批量分配，组员不可以', () => {
  assert.equal(canBulkAssignMobileTasks('组长', 'member'), true)
  assert.equal(canBulkAssignMobileTasks('组员', 'member'), false)
  for (const position of ['片长', '基础管控', '中队长', '所队领导']) {
    assert.equal(isFlowTaskElevated(position, 'member'), true, position)
    assert.equal(canAccessFlowTaskWorkbench(position, 'member'), true, position)
    assert.equal(canBulkAssignMobileTasks(position, 'member'), true, position)
  }
})

test('基础管控和中队长手机端自动分流，桌面端可用同一岗位准入', () => {
  assert.equal(isPoliceDispatchTaskPosition('基础管控'), true)
  assert.equal(isPoliceDispatchTaskPosition('中队长'), true)
  assert.equal(shouldUsePoliceDispatchWorkbench('基础管控', true), true)
  assert.equal(shouldUsePoliceDispatchWorkbench('中队长', false), false)
  assert.equal(shouldUsePoliceDispatchWorkbench('组长', true), false)
})

test('统一仪表盘使用新固定入口，旧在线汇总 ID 保持兼容', () => {
  const dashboard = navigationItemById('dashboard')!
  const summary = navigationItemById('online_summary')!
  const query = navigationItemById('online_query')!
  const flowTasks = navigationItemById('flow_tasks')!
  assert.equal(mobileNavigationItemLabel(dashboard, '组员', true), '首页')
  assert.equal(summary.path, '/summary')
  assert.equal(mobileNavigationItemLabel(summary, '组员', true), '在线汇总')
  assert.equal(mobileNavigationItemLabel(query, '组长'), '在线数据查询')
  assert.equal(mobileNavigationItemLabel(flowTasks, '组长'), '流口指令核查')
  assert.equal(routeIsActive('/tasks/全链条/row', query), false)
  assert.equal(routeIsActive('/tasks/全链条/row', flowTasks), true)
  assert.equal(routeIsActive('/police-tasks', query), false)
})

test('管理员手机导航使用独立流口任务入口', () => {
  const flowTasks = navigationItemById('flow_tasks')!
  const query = navigationItemById('online_query')!

  assert.equal(flowTasks.path, '/tasks/home')
  assert.equal(flowTasks.shortLabel, '流口核查')
  assert.equal(routeIsActive('/tasks/全链条/row', flowTasks), true)
  assert.equal(routeIsActive('/tasks/全链条/row', query), false)
})

test('批量保存只提交实际变化且不自动补造字段', () => {
  assert.deepEqual(buildMobileTaskChanges(
    { 核查人: '甲', 现住址: '', 核查结果: '' },
    { 核查人: '甲', 现住址: '长板一号', 核查结果: '', 研判: '不可编辑' },
    ['核查人', '现住址', '核查结果'],
  ), { 现住址: '长板一号' })
})

test('重复腾讯来源只列出真正不同的字段并保留空白差异', () => {
  const differences = mobileTaskSourceDifferences([
    { values: { 姓名: '朱明山', 电话: '13800000000', 现住址: '', 核查结果: '在吴' } },
    { values: { 姓名: '朱明山', 电话: '13800000000', 现住址: '长板一号', 核查结果: '离吴' } },
  ], ['姓名', '电话', '现住址', '核查结果'])

  assert.deepEqual(differences, [
    { field: '现住址', values: ['', '长板一号'] },
    { field: '核查结果', values: ['在吴', '离吴'] },
  ])
})

test('内容一致的重复来源不伪造差异', () => {
  assert.deepEqual(mobileTaskSourceDifferences([
    { values: { 姓名: '朱明山' } },
    { values: { 姓名: ' 朱明山 ' } },
  ], ['姓名']), [])
})

test('无法核实时才显示授权的二次反馈字段', () => {
  const detail = {
    workflow: {
      result_field: '核查结果',
      title_fields: [],
      secondary_fields: ['二次反馈'],
      phone_fields: [],
      address_fields: [],
      date_fields: [],
      columns: [],
    },
  }
  assert.deepEqual(
    mobileTaskEditorFields(detail, ['核查人', '核查结果', '二次反馈'], { 核查结果: '移交' }),
    ['核查人', '核查结果'],
  )
  assert.deepEqual(
    mobileTaskEditorFields(detail, ['核查人', '核查结果', '二次反馈'], { 核查结果: '无法核实' }),
    ['核查人', '核查结果', '二次反馈'],
  )
})

test('来源行保存后立即按业务真实口径更新状态', () => {
  assert.equal(
    mobileTaskSourceState('全链条', '核查结果', { 现住址: '长板一号' }),
    'checked',
  )
  assert.equal(
    mobileTaskSourceState('疑似未注销模型三', '核查结果', { 核查结果: '其他' }),
    'unchecked',
  )
  assert.equal(
    mobileTaskSourceState('疑似未注销模型三', '核查结果', { 核查结果: '在吴' }),
    'completed',
  )
  assert.equal(
    mobileTaskSourceNeedsReview('核查结果', ['二次反馈'], { 核查结果: '无法核实' }),
    true,
  )
})

test('有待办的业务优先，零任务业务沉底', () => {
  const base = { unchecked: 0, checked: 0, completed: 0, review: 0, source_ready: true }
  const sorted = sortMobileTaskBusinesses([
    { ...base, parser_type: 'b', label: '业务乙', pending: 0 },
    { ...base, parser_type: 'a', label: '业务甲', pending: 7 },
    { ...base, parser_type: 'c', label: '业务丙', pending: 2 },
  ])
  assert.deepEqual(sorted.map(item => item.parser_type), ['a', 'c', 'b'])
  assert.equal(mobileTaskPhoneValue('193-9261 0106'), '19392610106')
})

test('连续或分隔保存的多个手机号会拆成独立拨号选项', () => {
  assert.deepEqual(
    mobileTaskPhoneOptions('1556428608218549970040'),
    ['15564286082', '18549970040'],
  )
  assert.deepEqual(
    mobileTaskPhoneOptions('18856221510；0512-12345678；18856221510'),
    ['18856221510', '051212345678'],
  )
  assert.deepEqual(mobileTaskPhoneOptions('+86 18856221510'), ['18856221510'])
})

test('来源文本按空格和常用分隔符拆成去重标签', () => {
  assert.deepEqual(mobileTaskSourceTags('平安码 人像圈层'), ['平安码', '人像圈层'])
  assert.deepEqual(
    mobileTaskSourceTags('平安码、人像圈层 / 平安码'),
    ['平安码', '人像圈层'],
  )
})

test('只有手机浏览器会直接启动 tel 协议', () => {
  assert.equal(mobileTaskCanLaunchTelephone('Mozilla/5.0 (Linux; Android 15)'), true)
  assert.equal(mobileTaskCanLaunchTelephone('Mozilla/5.0 (Windows NT 10.0)'), false)
  assert.equal(mobileTaskCanLaunchTelephone('Mozilla/5.0 (Macintosh)', false, 5), true)
})

test('已研判任务在列表和详情直接显示研判结果', () => {
  const listSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )
  const detailSource = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
    'utf8',
  )
  assert.match(listSource, /task\.review_stage === 'analyzed' && task\.summary\.analysis/)
  assert.match(listSource, /研判结果/)
  assert.match(detailSource, /data\.workflow\.analysis_fields/)
  assert.match(detailSource, /研判结果/)
})

test('任务详情直接展示身份证号、手机号、来源和地址', () => {
  const detailSource = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
    'utf8',
  )
  assert.match(detailSource, /mobile-task-detail-facts/)
  for (const label of ['身份证号', '手机号', '来源', '原地址', '现住址']) {
    assert.equal(detailSource.includes(label), true, label)
  }
})

test('任务卡片使用可读密度、完整身份证主体和来源标签云', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )
  const styleSource = readFileSync(
    new URL('../src/index.css', import.meta.url),
    'utf8',
  )
  assert.match(pageSource, /mobile-task-item-card__identity/)
  assert.match(pageSource, /mobile-task-item-card__flags/)
  assert.match(pageSource, /mobile-task-source-cloud/)
  assert.match(pageSource, /mobileTaskSourceTags/)
  assert.match(styleSource, /repeat\(auto-fit, minmax\(236px, 1fr\)\)/)
  assert.match(styleSource, /\.mobile-task-item-card__identity[\s\S]*white-space: nowrap/)
  assert.doesNotMatch(styleSource, /repeat\(8, minmax\(0, 1fr\)\)/)
})

test('模型三备注会进入手机任务编辑字段', () => {
  const detail = {
    workflow: {
      result_field: '核查结果',
      title_fields: [],
      secondary_fields: [],
      extra_edit_fields: ['备注'],
      phone_fields: [],
      address_fields: [],
      date_fields: [],
      columns: [],
    },
  }
  assert.deepEqual(
    mobileTaskEditorFields(detail, ['核查结果', '备注'], { 核查结果: '', 备注: '' }),
    ['核查结果', '备注'],
  )
})

test('流口任务筛选使用 POST，关键词不进入 URL，数量卡和更多筛选可用', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )
  const clientSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )
  assert.match(clientSource, /api\.post\(\s*`\/mobile-tasks\//)
  assert.match(clientSource, /filter-options/)
  assert.match(clientSource, /communities: params\.communities \|\| \[\]/)
  assert.match(clientSource, /inspectors: params\.inspectors \|\| \[\]/)
  assert.match(pageSource, /mode="multiple"/)
  assert.match(pageSource, /getMobileTaskFilterOptions/)
  assert.match(pageSource, /priority_counts/)
  assert.match(pageSource, /更多筛选/)
  assert.match(pageSource, /setSearchParams\(next, \{ replace: true \}\)/)
  assert.equal(pageSource.includes("next.set('keyword'"), false)
})

test('流口任务数量卡顺序固定为已研判优先、已完成沉底', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )
  const analyzedIndex = pageSource.indexOf("{ key: 'analyzed'")
  const waitingIndex = pageSource.indexOf("{ key: 'waiting_analysis'")
  const completedIndex = pageSource.indexOf("{ key: 'completed'")
  assert.ok(analyzedIndex >= 0)
  assert.ok(waitingIndex > analyzedIndex)
  assert.ok(completedIndex > waitingIndex)
  assert.match(pageSource, /setReviewStage\('all'\)/)
})
