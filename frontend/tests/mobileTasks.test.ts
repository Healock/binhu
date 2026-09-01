import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  buildMobileTaskChanges,
  mergeMobileTaskSaveValues,
  mobileTaskEditorFields,
  mobileTaskCanLaunchTelephone,
  mobileTaskPhoneOptions,
  mobileTaskPhoneValue,
  mobileTaskSourceTags,
  formatMobileTaskDeadline,
  mobileTaskSourceDifferences,
  mobileTaskSourceNeedsReview,
  mobileTaskSourceState,
  mobileTaskSurfaceTone,
  mobileTaskCurrentAddressLabel,
  mobileTaskResultOptions,
  mobileTaskUsesRegistrationClosure,
  sortMobileTaskBusinesses,
} from '../src/utils/mobileTasks.ts'
import {
  readMobileTaskListRestoration,
  writeMobileTaskListRestoration,
} from '../src/utils/mobileTaskListState.ts'
import { retainAvailableMobileTaskFilters } from '../src/utils/mobileTaskFilters.ts'
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

test('任务截止日期统一显示为 MM-dd', () => {
  assert.equal(formatMobileTaskDeadline('2026-08-10'), '08-10')
  assert.equal(formatMobileTaskDeadline('8.10'), '08-10')
  assert.equal(formatMobileTaskDeadline('8.1'), '08-01')
  assert.equal(formatMobileTaskDeadline('8月10日'), '08-10')
  assert.equal(formatMobileTaskDeadline('待补充'), '待补充')
})

test('管理员和超级管理员可以进入流口岗任务工作台', () => {
  assert.equal(isFlowTaskAdmin('admin'), true)
  assert.equal(isFlowTaskAdmin('member', ['admin']), true)
  assert.equal(canAccessFlowTaskWorkbench('', 'super_admin'), true)
  assert.equal(canAccessFlowTaskWorkbench('社区民警', 'member', []), true)
  assert.equal(canAccessFlowTaskWorkbench('', 'member', [], ['online.task.manage']), true)
})

test('组长及上级任务岗位可以批量分配，组员不可以', () => {
  assert.equal(canBulkAssignMobileTasks('组长', 'member'), true)
  assert.equal(canBulkAssignMobileTasks('组员', 'member'), false)
  for (const position of ['片长', '基础管控', '中队长', '社区民警', '所队领导']) {
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
  assert.equal(flowTasks.shortLabel, '指令核查')
  assert.equal(navigationItemById('police_tasks')?.shortLabel, '任务下发')
  assert.equal(routeIsActive('/tasks/全链条/row', flowTasks), true)
  assert.equal(routeIsActive('/tasks/全链条/row', query), false)
})

test('任务首页需复核徽标进入统一复核筛选而不是来源异常筛选', () => {
  const homeSource = readFileSync(
    new URL('../src/pages/MobileTaskHome.tsx', import.meta.url),
    'utf8',
  )
  assert.match(homeSource, /需复核 \{item\.review\}/)
  assert.match(homeSource, /scope=\$\{scope\}&status=review/)
  assert.match(homeSource, /mobile-task-review-badge/)
  assert.doesNotMatch(homeSource, /异常来源 \{item\.review\}/)
  assert.match(homeSource, /role="button"/)

  const styleSource = readFileSync(
    new URL('../src/index.css', import.meta.url),
    'utf8',
  )
  assert.match(styleSource, /\.mobile-task-review-badge\s*\{[\s\S]*min-height:\s*22px/)
  assert.match(styleSource, /mobile-task-review-badge\) \{\s*min-height: 36px/)
  assert.match(styleSource, /mobile-task-review-badge\) \{\s*min-height: 44px/)
})

test('批量保存只提交实际变化且不自动补造字段', () => {
  assert.deepEqual(buildMobileTaskChanges(
    { 核查人: '甲', 现住址: '', 核查结果: '' },
    { 核查人: '甲', 现住址: '长板一号', 核查结果: '', 研判: '不可编辑' },
    ['核查人', '现住址', '核查结果'],
  ), { 现住址: '长板一号' })
})

test('登记闭环的地址标题和核查结果选项使用统一口径', () => {
  assert.equal(mobileTaskCurrentAddressLabel('全链条', ''), '核查补充信息')
  assert.equal(mobileTaskCurrentAddressLabel('全链条', '待登记'), '现住址')
  assert.equal(mobileTaskCurrentAddressLabel('疑似未注销模型三', '待登记'), '现住址')

  const options = [
    { id: 'legacy-transfer', text: '移交' },
    { id: 'internal-transfer', text: '移交（所内）' },
    { id: 'external-transfer', text: '移交（所外）' },
    { id: 'registered', text: '已登记' },
    { id: 'pending-registration', text: '待登记' },
  ]
  assert.deepEqual(
    mobileTaskResultOptions(options, true).map(option => option.text),
    ['移交（所内）', '移交（所外）', '待登记'],
  )
})

test('行内待登记直接搜索任务社区房屋并原子保存', () => {
  const source = readFileSync(
    new URL('../src/components/MobileTaskTable.tsx', import.meta.url),
    'utf8',
  )
  assert.match(source, /searchRegistrationProperties\(normalized, task\.community\)/)
  assert.match(source, /\[resultField\]: '待登记'[\s\S]*?现住址: address/)
  assert.match(source, /registration_property_id: registrationProperty\.id/)
  assert.match(source, /registration_property_version: registrationProperty\.version/)
  assert.match(source, /function registrationPropertyLabel\([\s\S]*?return registrationPropertyAddress\(property\)/)
  assert.doesNotMatch(source, /return `\$\{property\.community_name \|\| ''\} \$\{registrationPropertyAddress\(property\)\}`\.trim\(\)/)
  assert.match(source, /选定房屋后，待登记结果和现住址会一次保存/)
  assert.doesNotMatch(source, /待登记需进入详情/)

  const detailSource = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
    'utf8',
  )
  assert.match(detailSource, /label: `\$\{property\.natural_address \|\| ''\}\$\{property\.building \|\| ''\}\$\{property\.room \|\| ''\}`\.trim\(\)/)
  assert.doesNotMatch(detailSource, /label: `\$\{property\.community_name \|\| ''\} /)
})

test('任务详情保存合并响应时保留下拉结果并把选项 ID 还原为文本', () => {
  const source = { 核查人: '甲', 现住址: '长板一号', 核查结果: '' }
  const changes = { 核查结果: '已登记' }
  const meta = {
    核查结果: {
      type: 'select',
      options: [{ id: 'result-1', text: '已登记' }],
    },
  }

  assert.equal(
    mergeMobileTaskSaveValues(source, changes, { ...source, 核查结果: 'result-1' }, meta).核查结果,
    '已登记',
  )
  assert.equal(
    mergeMobileTaskSaveValues(source, changes, { ...source }, meta).核查结果,
    '已登记',
  )
  assert.equal(
    mergeMobileTaskSaveValues(
      { ...source, 核查结果: '已登记' },
      { 核查结果: '' },
      { ...source, 核查结果: '' },
      meta,
    ).核查结果,
    '',
  )
})

test('任务详情保存未分配任务时复用自主领取接口和确认提示', () => {
  const source = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
    'utf8',
  )
  assert.match(source, /claimMobileTask/)
  assert.match(source, /该任务暂未分配核查人，是否领取任务？/)
  assert.match(source, /okText: '领取并保存'/)
  assert.match(source, /未领取任务，填写内容未保存/)
  assert.match(source, /const updater = claim\s*\?\s*claimMobileTask/)
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
  assert.deepEqual(
    mobileTaskEditorFields(
      detail,
      ['核查人', '核查结果', '二次反馈'],
      { 核查结果: '已登记', 二次反馈: '重新联系后可以登记' },
      { 核查结果: '无法核实', 二次反馈: '' },
    ),
    ['核查人', '核查结果', '二次反馈'],
  )
})

test('最终结果保存后保留二次反馈只读记录', () => {
  const detailSource = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
    'utf8',
  )
  assert.match(detailSource, /preservedSecondaryFeedback/)
  assert.match(detailSource, /\{item\.field\}记录/)
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
    mobileTaskSourceState('疑似未注销模型三', '核查结果', { 核查结果: '离吴' }),
    'completed',
  )
  assert.equal(
    mobileTaskSourceState('疑似未注销模型三', '核查结果', { 核查结果: '近期返吴' }),
    'completed',
  )
  assert.equal(
    mobileTaskSourceState('疑似未注销模型三', '核查结果', { 核查结果: '近期反吴' }),
    'completed',
  )
  assert.equal(
    mobileTaskSourceState('疑似未注销模型三', '核查结果', { 核查结果: '非本辖区' }),
    'completed',
  )
  assert.equal(
    mobileTaskSourceNeedsReview('核查结果', ['二次反馈'], { 核查结果: '无法核实' }),
    true,
  )
})

test('任务面板按未分配、核查进度、移交和研判待复核映射业务颜色', () => {
  const task = (overrides: Partial<{
    inspector: string
    state: 'unchecked' | 'checked' | 'completed'
    review_stage: '' | 'waiting_analysis' | 'analyzed'
    result: string
    secondary_feedback: string
  }> = {}) => ({
    inspector: overrides.inspector ?? '张三',
    state: overrides.state ?? 'unchecked',
    review_stage: overrides.review_stage ?? '',
    summary: {
      result: overrides.result ?? '',
      secondary_feedback: overrides.secondary_feedback ?? '',
    },
  })

  assert.equal(mobileTaskSurfaceTone(task({ inspector: '' })), 'unassigned')
  assert.equal(mobileTaskSurfaceTone(task()), 'unchecked')
  assert.equal(mobileTaskSurfaceTone(task({ state: 'checked' })), 'checked')
  assert.equal(mobileTaskSurfaceTone(task({ state: 'completed' })), 'completed')
  assert.equal(mobileTaskSurfaceTone(task({ inspector: '', state: 'completed' })), 'completed')
  assert.equal(mobileTaskSurfaceTone(task({ state: 'completed', result: '移交其他社区' })), 'transfer')
  assert.equal(mobileTaskSurfaceTone(task({
    state: 'completed',
    review_stage: 'analyzed',
    result: '无法核实',
  })), 'analysis-review')
  assert.equal(mobileTaskSurfaceTone(task({
    state: 'completed',
    review_stage: 'analyzed',
    result: '无法核实',
    secondary_feedback: '已补充说明',
  })), 'completed')
  assert.equal(mobileTaskSurfaceTone(task({
    state: 'completed',
    review_stage: 'waiting_analysis',
    result: '无法核实',
  })), 'completed')
})

test('任务列表返回记录按视图和有效期恢复，不因返回路由细节变化而丢失', () => {
  const values = new Map<string, string>()
  const storage = {
    getItem: (key: string) => values.get(key) || null,
    setItem: (key: string, value: string) => { values.set(key, value) },
    removeItem: (key: string) => { values.delete(key) },
  }
  const savedAt = 1_000_000
  writeMobileTaskListRestoration(storage, {
    version: 1,
    mode: 'tasks',
    return_url: '/tasks?type=流口指令核查',
    display_mode: 'card',
    scroll_top: 1820,
    page: 4,
    loaded_page: 4,
    keyword: '测试姓名',
    row_key: 'row-42',
    saved_at: savedAt,
  })

  assert.deepEqual(
    readMobileTaskListRestoration(
      storage,
      'tasks',
      'card',
      savedAt + 1000,
    ),
    {
      version: 1,
      mode: 'tasks',
      return_url: '/tasks?type=流口指令核查',
      display_mode: 'card',
      scroll_top: 1820,
      page: 4,
      loaded_page: 4,
      keyword: '测试姓名',
      row_key: 'row-42',
      saved_at: savedAt,
    },
  )
  assert.equal(readMobileTaskListRestoration(storage, 'analysis', 'card', savedAt + 1000), null)
})

test('有待办的业务优先，零任务业务沉底', () => {
  const base = { unchecked: 0, checked: 0, completed: 0, review: 0, source_ready: true }
  const sorted = sortMobileTaskBusinesses([
    { ...base, parser_type: 'b', label: '业务乙', pending: 0 },
    { ...base, parser_type: 'a', label: '业务甲', pending: 7 },
    { ...base, parser_type: 'c', label: '业务丙', pending: 2 },
  ])
  assert.deepEqual(sorted.map(item => item.parser_type), ['a', 'c', 'b'])
  assert.equal(mobileTaskPhoneValue('123-4567 8901'), '12345678901')
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
  assert.match(listSource, /\['analyzed', 'initial_extension', 'deep_pending', 'deep_extension'\]\.includes\(task\.review_stage\) && task\.summary\.analysis/)
  assert.match(listSource, /研判结果/)
  assert.match(detailSource, /data\.workflow\.analysis_fields/)
  assert.match(detailSource, /研判结果/)
})

test('两级研判状态在卡片、表格和详情中直接提示复核信息', () => {
  const listSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )
  const tableSource = readFileSync(
    new URL('../src/components/MobileTaskTable.tsx', import.meta.url),
    'utf8',
  )
  const detailSource = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
    'utf8',
  )
  const noticeSource = readFileSync(
    new URL('../src/components/UnverifiableReviewNotice.tsx', import.meta.url),
    'utf8',
  )
  for (const source of [listSource, tableSource, detailSource]) {
    assert.match(source, /UnverifiableReviewNotice/)
  }
  assert.match(noticeSource, /复核截止/)
  assert.match(noticeSource, /本轮反馈/)
  assert.match(noticeSource, /核实后请及时更新核查结果/)
  assert.match(noticeSource, /只填写二次反馈不会结束无法核实流程/)
})

test('流口任务和待研判任务按当前筛选排序导出并支持研判结果回导', () => {
  const listSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )
  const apiSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(apiSource, /mobileTaskSearchPayload\(params\)/)
  assert.match(apiSource, /\/mobile-tasks\/analysis\/export/)
  assert.match(apiSource, /\/mobile-tasks\/analysis\/import/)
  assert.match(listSource, /exportMobileTasks\(\{[\s\S]*?sort,[\s\S]*?keyword:/)
  assert.match(listSource, /exportMobileTaskAnalysis\(\{[\s\S]*?sort,[\s\S]*?keyword:/)
  assert.match(listSource, /导出当前结果/)
  assert.match(listSource, /导入研判结果/)
})

test('研判详情只提供结构化成功失败决定，不再自由保存研判文字', () => {
  const detailSource = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
    'utf8',
  )
  const tableSource = readFileSync(
    new URL('../src/components/MobileTaskTable.tsx', import.meta.url),
    'utf8',
  )
  assert.match(detailSource, /decideMobileTaskUnverifiableReview/)
  assert.match(detailSource, /研判成功（进入延时复核）/)
  assert.match(detailSource, /研判失败（进入下一阶段）/)
  assert.match(detailSource, /提交本阶段研判/)
  assert.match(tableSource, /两级研判必须在详情中选择成功或失败并填写意见/)
  assert.match(tableSource, /进入研判详情/)
})

test('研判 URL 可以恢复四种两级研判阶段', () => {
  const listSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )
  for (const stage of ['initial_pending', 'initial_extension', 'deep_pending', 'deep_extension']) {
    assert.match(listSource, new RegExp(`'${stage}'`))
  }
  assert.match(listSource, /selectableReviewStages\.includes\(requestedReviewStage/)
})

test('全链条新增待登记结果保留为正式任务选项', () => {
  assert.equal(
    mobileTaskSourceState('全链条', '核查结果', { 核查结果: '待登记' }),
    'checked',
  )
})

test('待登记不会被前端误判为已完成', () => {
  for (const parserType of ['全链条', '出租房屋核查', '寄递业', '疑似返苏', '苏州涉警', '交通涉警']) {
    assert.equal(
      mobileTaskSourceState(parserType, '核查结果', { 核查结果: '待登记', 现住址: '拟登记地址' }),
      'checked',
      parserType,
    )
  }
})

test('详情页按待登记状态动态显示现住址或核查补充信息', () => {
  const detailSource = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
    'utf8',
  )
  assert.match(detailSource, /mobileTaskCurrentAddressLabel\(/)
  assert.match(detailSource, /mobileTaskUsesRegistrationClosure\(parserType\)/)
  assert.doesNotMatch(detailSource, /parserType !== '疑似未注销模型三'/)
})

test('六类闭环业务统一使用动态住址标题', () => {
  assert.equal(mobileTaskCurrentAddressLabel('全链条', '待登记'), '现住址')
  assert.equal(mobileTaskCurrentAddressLabel('全链条', '无法核实'), '核查补充信息')
  assert.equal(mobileTaskCurrentAddressLabel('疑似未注销模型三', '待登记'), '现住址')
})

test('登记房屋闭环只用于六类指令核查业务', () => {
  for (const parserType of ['全链条', '出租房屋核查', '寄递业', '疑似返苏', '苏州涉警', '交通涉警']) {
    assert.equal(mobileTaskUsesRegistrationClosure(parserType), true)
  }
  assert.equal(mobileTaskUsesRegistrationClosure('疑似未注销模型三'), false)
  assert.equal(mobileTaskUsesRegistrationClosure('未知业务'), false)
})

test('表格行内编辑允许待登记但必须与房屋一次保存', () => {
  const tableSource = readFileSync(new URL('../src/components/MobileTaskTable.tsx', import.meta.url), 'utf8')
  assert.match(tableSource, /mobileTaskResultOptions\(metadata\.options, registrationResultField\)/)
  assert.match(tableSource, /searchRegistrationProperties\(normalized, task\.community\)/)
  assert.match(tableSource, /registration_property_id: registrationProperty\.id/)
  assert.match(tableSource, /registration_property_version: registrationProperty\.version/)
  assert.doesNotMatch(tableSource, /待登记需进入详情/)
})

test('任务列表和详情展示登记比对阶段与复核原因', () => {
  const listSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )
  const tableSource = readFileSync(
    new URL('../src/components/MobileTaskTable.tsx', import.meta.url),
    'utf8',
  )
  const detailSource = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
    'utf8',
  )
  const statusSource = readFileSync(
    new URL('../src/components/RegistrationLinkStatus.tsx', import.meta.url),
    'utf8',
  )
  assert.match(listSource, /RegistrationLinkStatus link=\{task\.registration_link\}/)
  assert.match(tableSource, /RegistrationLinkStatus link=\{task\.registration_link\}/)
  assert.match(detailSource, /待登记房屋关联/)
  assert.match(detailSource, /两个独立扫描周期/)
  assert.match(statusSource, /登记待复核/)
  assert.match(statusSource, /已匹配一次/)
  assert.match(listSource, /登记复核（\$\{facets\.registration_review_count\}）/)
  assert.match(listSource, /registration_review/)
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

test('任务详情不再展示调取照片信息', () => {
  const detailSource = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
    'utf8',
  )
  const clientSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.doesNotMatch(detailSource, /已调取照片/)
  assert.doesNotMatch(detailSource, /workflowApi\.attachmentUrl\(request\.ticket_id, attachment\.file_id, true\)/)
  assert.doesNotMatch(detailSource, /<DownloadOutlined/)
  assert.match(clientSource, /photo_requests: Array/)
})

test('照片完成通知允许已读后继续跳回原任务且拒绝外部路径', () => {
  const notificationSource = readFileSync(
    new URL('../src/components/NotificationCenter.tsx', import.meta.url),
    'utf8',
  )

  assert.match(notificationSource, /notification\.action_path/)
  assert.match(notificationSource, /\^\\\/\(\?!\\\/\)/)
  assert.match(notificationSource, /navigate\(notification\.action_path\)/)
  assert.doesNotMatch(notificationSource, /if \(notification\.is_read\) return/)
})

test('任务详情文本编辑器已从 Ant Design 引入 Input', () => {
  const detailSource = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
    'utf8',
  )
  const antdImport = detailSource.match(/import\s*\{([^}]*)\}\s*from 'antd'/s)?.[1] || ''
  assert.match(antdImport, /\bInput\b/)
  assert.match(detailSource, /<Input\.TextArea/)
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
  assert.match(pageSource, /mobile-task-item-card__key-info/)
  assert.match(pageSource, /task\.summary\.current_address/)
  assert.match(pageSource, /task\.summary\.original_address/)
  assert.match(pageSource, /mobileTaskCurrentAddressLabel\(task\.parser_type, task\.summary\.result \|\| ''\)/)
  assert.match(pageSource, /mobile-task-item-card__key-row--old-address/)
  assert.match(pageSource, /<dt>原地址<\/dt>/)
  assert.match(pageSource, /mobile-task-item-card__flags/)
  assert.doesNotMatch(pageSource, /task\.photo_fetched/)
  assert.doesNotMatch(pageSource, /已调照片/)
  assert.match(pageSource, /mobile-task-source-cloud/)
  assert.match(pageSource, /mobileTaskSourceTags/)
  assert.match(pageSource, /copyCardValue/)
  assert.match(pageSource, /身份证号已复制|`\$\{label\}已复制`/)
  assert.match(pageSource, /event\.target !== event\.currentTarget/)
  assert.equal(pageSource.includes('RightOutlined'), false)
  assert.match(pageSource, /mobile-task-item-card__state/)
  assert.match(pageSource, /mode="copy"/)
  assert.match(pageSource, /extraPhoneCount/)
  assert.match(pageSource, /mobile-task-item-card__phone-extra/)
  assert.match(pageSource, /mobile-task-item-card__footer-meta/)
  assert.match(pageSource, /<span title=\{task\.inspector \|\| '待分配'\}>\{task\.inspector \|\| '待分配'\}<\/span>/)
  assert.equal(pageSource.includes('核查人 {task.inspector'), false)
  assert.equal(pageSource.includes('<Checkbox'), false)
  assert.match(pageSource, /分配数据/)
  assert.match(pageSource, /MobileTaskAssignmentWorkbench/)
  assert.doesNotMatch(pageSource, /const \[selectionMode, setSelectionMode\]/)
  assert.doesNotMatch(pageSource, /selectMobileTasksForAssignment/)
  assert.doesNotMatch(pageSource, /bulkMode/)
  assert.match(pageSource, /setInterval\(refreshVisibleList, 30_000\)/)
  assert.match(pageSource, /visibilitychange/)
  assert.doesNotMatch(pageSource, /selectAllLoaded/)
  assert.match(pageSource, /formatMobileTaskDeadline/)
  assert.ok(
    pageSource.indexOf('mobile-task-analysis') < pageSource.indexOf('mobile-task-source-cloud mobile-task-source-cloud--card'),
  )
  assert.equal(pageSource.includes('<span>来源</span>'), false)
  assert.match(styleSource, /repeat\(auto-fit, minmax\(264px, 1fr\)\)/)
  assert.match(styleSource, /\.mobile-task-item-card__identity[\s\S]*white-space: nowrap/)
  assert.match(styleSource, /\.mobile-task-copy-value/)
  assert.match(styleSource, /\.mobile-task-copy-value\s*\{[\s\S]*display:\s*inline-flex[\s\S]*width:\s*auto[\s\S]*justify-content:\s*flex-start/)
  assert.match(styleSource, /\.mobile-task-copy-value:hover,[\s\S]*\.mobile-task-copy-value:focus-visible[\s\S]*background:\s*var\(--app-surface-muted\)/)
  assert.match(styleSource, /--mobile-task-footer-bg:\s*#eef3f8/)
  assert.match(styleSource, /html\[data-theme='dark'\][\s\S]*--mobile-task-footer-bg:\s*#0b1320/)
  assert.match(styleSource, /\.mobile-task-item-card__footer[\s\S]*background:\s*var\(--mobile-task-footer-bg\)/)
  assert.match(styleSource, /\.mobile-task-copy-value \.anticon[\s\S]*color:\s*var\(--app-text-muted\)/)
  assert.match(styleSource, /\.mobile-task-list\s*\{[\s\S]*align-items:\s*stretch/)
  assert.match(styleSource, /\.mobile-task-item-card\s*\{[\s\S]*display:\s*flex[\s\S]*flex-direction:\s*column/)
  assert.match(styleSource, /\.mobile-task-item-card__body\s*\{[\s\S]*flex:\s*1[\s\S]*flex-direction:\s*column/)
  assert.match(styleSource, /\.mobile-task-source-cloud--card\s*\{[\s\S]*margin-top:\s*auto/)
  assert.match(pageSource, /mobileTaskSurfaceTone\(task\)/)
  assert.match(pageSource, /mobile-task-item-card--tone-\$\{surfaceTone\}/)
  assert.match(styleSource, /--mobile-task-panel-shadow:/)
  assert.match(styleSource, /mobile-task-item-card--tone-completed[\s\S]*var\(--app-success\)/)
  assert.match(styleSource, /mobile-task-item-card--tone-checked[\s\S]*var\(--app-warning\)/)
  assert.match(styleSource, /mobile-task-item-card--tone-transfer[\s\S]*var\(--app-danger\)/)
  assert.match(styleSource, /mobile-task-item-card--tone-transfer[\s\S]*border-inline-start:\s*3px solid var\(--app-danger\)/)
  assert.match(styleSource, /mobile-task-item-card--tone-unassigned[\s\S]*var\(--app-status-neutral\)/)
  assert.match(styleSource, /mobile-task-item-card--tone-unchecked[\s\S]*var\(--app-status-unchecked\)/)
  assert.match(styleSource, /mobile-task-item-card--tone-analysis-review[\s\S]*var\(--app-status-analysis-review\)/)
  assert.match(styleSource, /mobile-task-item-card--tone-completed[\s\S]*var\(--app-success\) 2%/)
  assert.match(styleSource, /\.mobile-task-assignment-workbench\s*\{[\s\S]*height:\s*100%/)
  assert.match(styleSource, /\.mobile-task-item-card__title-row h2[\s\S]*min-width:\s*0/)
  assert.match(styleSource, /\.mobile-task-item-card__title-row h2[\s\S]*font-size:\s*20px/)
  assert.match(pageSource, /mobile-task-item-card__key-row--phone/)
  assert.match(pageSource, /className="mobile-phone-native-select--card"/)
  assert.ok(
    pageSource.indexOf('mobile-task-item-card__key-row--identity')
      < pageSource.indexOf('mobile-task-item-card__key-row--phone'),
  )
  assert.match(styleSource, /\.mobile-task-item-card__phone-copy\.ant-btn[\s\S]*font-variant-numeric:\s*tabular-nums/)
  assert.match(styleSource, /\.mobile-task-item-card__phone-copy\.ant-btn[\s\S]*font-weight:\s*650/)
  assert.match(styleSource, /\.mobile-phone-native-select--card\s*\{[\s\S]*width:\s*auto/)
  assert.match(styleSource, /\.mobile-task-item-card__phone-copy\.ant-btn[\s\S]*justify-content:\s*flex-start/)
  assert.equal(pageSource.includes('mobile-phone-native-select--header'), false)
  assert.match(styleSource, /\.mobile-task-item-card__footer-meta[\s\S]*font-size:\s*12px/)
  assert.match(styleSource, /\.mobile-task-item-card__key-row--old-address dt,[\s\S]*color:\s*var\(--app-text-muted\)[\s\S]*font-size:\s*12px/)
  const keyInfoStyle = styleSource.match(/\.mobile-task-item-card__key-info\s*\{([^}]*)\}/)?.[1] || ''
  assert.doesNotMatch(keyInfoStyle, /background|border/)
  assert.match(keyInfoStyle, /gap:\s*2px/)
  assert.match(styleSource, /\.mobile-task-copy-value\s*\{[\s\S]*height:\s*22px/)
  assert.match(styleSource, /\.mobile-task-item-card__phone-copy\.ant-btn\s*\{[\s\S]*height:\s*22px/)
  assert.match(styleSource, /\.mobile-task-item-card__key-row\s*\{[\s\S]*min-height:\s*24px[\s\S]*align-items:\s*center/)
  assert.match(styleSource, /\.mobile-task-item-card__key-row dd\s*\{[\s\S]*font-weight:\s*650[\s\S]*line-height:\s*22px/)
  assert.match(styleSource, /\.mobile-task-item-card__key-row--address dd\s*\{[\s\S]*font-weight:\s*650[\s\S]*line-height:\s*22px/)
  assert.doesNotMatch(styleSource, /repeat\(8, minmax\(0, 1fr\)\)/)
})

test('流口任务支持账号级表格视图并在手机端保留卡片', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )
  const tableSource = readFileSync(
    new URL('../src/components/MobileTaskTable.tsx', import.meta.url),
    'utf8',
  )
  const clientSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )
  const styleSource = readFileSync(
    new URL('../src/index.css', import.meta.url),
    'utf8',
  )
  const settingsSource = readFileSync(
    new URL('../src/pages/PersonalizationSettings.tsx', import.meta.url),
    'utf8',
  )
  const editorRequestSource = tableSource.slice(
    tableSource.indexOf('const requestEditors'),
    tableSource.indexOf('const queueEditorLoad'),
  )

  assert.match(pageSource, /user\?\.task_display_mode \|\| 'table'/)
  assert.match(pageSource, /<MobileTaskTable/)
  assert.match(pageSource, /className="hidden md:block"/)
  assert.match(pageSource, /taskDisplayMode === 'table' \? ' mobile-task-list--table-fallback' : ''/)
  assert.match(styleSource, /@media \(min-width: 768px\)[\s\S]*mobile-task-list\.mobile-task-list--table-fallback[\s\S]*display: none/)
  assert.equal(pageSource.includes("taskDisplayMode === 'table' ? ' md:hidden'"), false)
  assert.match(pageSource, /if \(silent \|\| restorePageCount > 0\)[\s\S]*requestedPage <= refreshPageCount/)
  assert.match(pageSource, /writeMobileTaskListRestoration\(window\.sessionStorage/)
  assert.match(pageSource, /scroll_top: scrollContainer\?\.scrollTop \|\| window\.scrollY/)
  assert.match(pageSource, /loaded_page: loadedPageRef\.current/)
  assert.match(pageSource, /void load\([\s\S]*restoration\.page[\s\S]*restoration\.loaded_page,?[\s\S]*\)/)
  assert.match(pageSource, /writeMobileTaskListSnapshot\(\{/)
  assert.match(pageSource, /snapshotRef\.current\?\.rows/)
  assert.match(pageSource, /scrollContainer\.scrollTop = Math\.min\(restoration\.scroll_top, maxScrollTop\)/)
  assert.match(pageSource, /data-mobile-task-row-key=\{task\.task_key\}/)
  assert.match(tableSource, /Table<MobileTaskItem>/)
  assert.match(tableSource, /title: '截止日期'/)
  assert.match(tableSource, /title: '社区'/)
  assert.match(tableSource, /dataIndex: 'community'/)
  assert.match(tableSource, /未识别社区/)
  assert.match(tableSource, /title: '登记情况'/)
  assert.match(tableSource, /expandedRowRender/)
  assert.match(tableSource, /mobile-task-table-primary-row/)
  assert.match(tableSource, /mobileTaskSurfaceTone\(task\)/)
  assert.match(tableSource, /mobile-task-table-primary-row--tone-\$\{mobileTaskSurfaceTone\(task\)\}/)
  assert.match(tableSource, /'data-mobile-task-row-key': task\.task_key/)
  assert.match(tableSource, /mobile-task-table-inline-editor--tone-\$\{surfaceTone\}/)
  assert.doesNotMatch(tableSource, /编辑本行/)
  assert.match(tableSource, /getMobileTaskInlineEditors/)
  assert.match(tableSource, /const grouped = keys\.reduce/)
  assert.match(tableSource, /taskByKey\.get\(taskKey\)/)
  assert.match(tableSource, /getMobileTaskInlineEditors\(parserType, parserTasks\.map\(task => task\.rowKey\), analysisMode\)/)
  assert.match(tableSource, /new IntersectionObserver/)
  assert.match(tableSource, /rootMargin: '600px 0px'/)
  assert.match(tableSource, /queueEditorLoad\(rowKey\)/)
  assert.doesNotMatch(editorRequestSource, /rows\.map\(task => task\.row_key\)/)
  assert.match(clientSource, /for \(let offset = 0; offset < uniqueRowKeys\.length; offset \+= 50\)/)
  assert.match(clientSource, /uniqueRowKeys\.slice\(offset, offset \+ 50\)/)
  assert.match(tableSource, /updateMobileTask/)
  assert.match(tableSource, /claimMobileTask/)
  assert.match(tableSource, /该任务暂未分配核查人，是否领取任务？/)
  assert.match(tableSource, /okText: '领取并保存'/)
  assert.match(tableSource, /const claim = await confirmClaim\(task, source\.values\)/)
  assert.match(tableSource, /await saveEditor\(task, item, changes, claim\)/)
  assert.match(tableSource, /\[field\]: source\.values\[field\] \|\| ''/)
  assert.match(pageSource, /canClaimUnassigned=\{!analysisOnly && user\?\.member\?\.position === '组员'\}/)
  assert.match(clientSource, /source-rows\/\$\{sourceId\}\/claim/)
  assert.match(tableSource, /const visiblePhones = phones\.slice\(0, 3\)/)
  assert.match(tableSource, /phones\.length - visiblePhones\.length/)
  assert.match(tableSource, /const saveField = async/)
  assert.match(tableSource, /cancelScheduledFieldSave\(task\.task_key, field\)/)
  assert.match(tableSource, /void saveField\(task, item, field, values\[field\] \|\| ''\)/)
  assert.doesNotMatch(tableSource, /保存 \$\{dirtyCount\} 项/)
  assert.doesNotMatch(tableSource, /title="查看任务"/)
  assert.match(tableSource, /<Tooltip title=\{task\.summary\.analysis \|\| '未填写'\}>/)
  assert.match(tableSource, /<Tooltip title=\{task\.summary\.analysis\}>/)
  assert.match(tableSource, /mobileTaskEditorFields/)
  assert.match(tableSource, /placeholder="请选择"/)
  assert.match(tableSource, /placeholder=\{field === '入住方式' \? '自购、房东出租、中介出租等' : '请输入'\}/)
  assert.match(tableSource, /mobileTaskCurrentAddressLabel\(task\.parser_type, task\.summary\.result \|\| ''\)/)
  assert.match(tableSource, />核查结果</)
  assert.match(tableSource, />研判</)
  assert.match(tableSource, />二次反馈</)
  assert.doesNotMatch(tableSource, />调取照片</)
  assert.match(tableSource, /rows\.some\(task => task\.parser_type === '全链条'\)/)
  assert.match(tableSource, /hideSelectAll: true/)
  assert.match(tableSource, /pagination=\{false\}/)
  assert.doesNotMatch(pageSource, /new IntersectionObserver/)
  assert.doesNotMatch(pageSource, /rootMargin: '720px 0px'/)
  assert.match(pageSource, /scrollLoadArmedRef/)
  assert.match(pageSource, /remaining > 80/)
  assert.match(pageSource, /scrollLoadArmedRef\.current = false[\s\S]*load\(page \+ 1, true\)/)
  assert.match(pageSource, /继续向下滑到底部加载下一批/)
  assert.match(pageSource, /useNavigationType/)
  assert.match(pageSource, /navigationType === 'POP'/)
  assert.match(tableSource, /mobile-task-source-cloud mobile-task-source-cloud--table/)
  assert.match(tableSource, /mobileTaskSourceTags\(task\.summary\.source\)[\s\S]*sources\.map\(tag =>/)
  assert.match(styleSource, /mobile-task-source-cloud--table[\s\S]*margin-top: 0/)
  assert.match(styleSource, /\.mobile-task-table\s*\{[\s\S]*padding: 0 10px 10px/)
  assert.match(styleSource, /mobile-task-table-primary-row > td:first-child[\s\S]*border-start-start-radius: 12px/)
  assert.match(styleSource, /mobile-task-table-primary-row > td:last-child[\s\S]*border-start-end-radius: 12px/)
  assert.match(styleSource, /ant-table-expanded-row > td[\s\S]*padding: 0 0 10px/)
  assert.match(styleSource, /ant-table-expanded-row > td[\s\S]*position: relative[\s\S]*padding: 0 0 10px/)
  assert.match(styleSource, /ant-table-expanded-row-fixed[\s\S]*position: static !important[\s\S]*inset: auto !important[\s\S]*left: auto !important[\s\S]*width: 100% !important[\s\S]*max-width: none !important[\s\S]*margin: 0 !important[\s\S]*padding: 0 !important[\s\S]*overflow: visible !important[\s\S]*transform: none !important/)
  assert.match(styleSource, /mobile-task-table-inline-editor\s*\{[\s\S]*border-radius: 0 0 12px 12px/)
  assert.match(styleSource, /mobile-task-table-inline-editor\s*\{[\s\S]*box-shadow: var\(--mobile-task-panel-shadow\)/)
  assert.match(styleSource, /mobile-task-table-primary-row > td:first-child[\s\S]*border-left:\s*3px solid var\(--mobile-task-row-accent/)
  assert.match(styleSource, /mobile-task-table-inline-editor\s*\{[\s\S]*border-inline-start:\s*3px solid var\(--mobile-task-row-accent/)
  assert.match(styleSource, /mobile-task-table-primary-row--tone-completed[\s\S]*--mobile-task-row-bg:[\s\S]*var\(--app-success\) 2%/)
  assert.match(styleSource, /mobile-task-table-primary-row--tone-transfer[\s\S]*var\(--app-danger\) 2%/)
  assert.match(styleSource, /mobile-task-table-primary-row--tone-analysis-review[\s\S]*var\(--app-status-analysis-review\) 2%/)
  assert.doesNotMatch(styleSource, /inset 3px 0 0 color-mix/)
  assert.match(styleSource, /mobile-task-table-inline-fields/)
  assert.match(styleSource, /mobile-task-table-inline-editor--dirty/)
  assert.match(styleSource, /mobile-task-table-row-selected \+ \.ant-table-expanded-row[\s\S]*border-color: var\(--app-primary\)/)
  assert.match(settingsSource, /流口任务展示/)
  assert.match(settingsSource, /useState<TaskDisplayMode>\('table'\)/)
  assert.match(settingsSource, /task_display_mode: taskDisplayMode/)
  assert.match(settingsSource, /卡片视图/)
  assert.match(settingsSource, /表格视图/)
})

test('分配数据使用独立全屏工作台，只展示来源和地址', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )
  const workbenchSource = readFileSync(
    new URL('../src/components/MobileTaskAssignmentWorkbench.tsx', import.meta.url),
    'utf8',
  )
  const clientSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(pageSource, /<MobileTaskAssignmentWorkbench/)
  assert.match(workbenchSource, /getMobileTaskAssignmentWorkbench/)
  assert.match(workbenchSource, /按地址排序，只展示来源和地址/)
  assert.match(workbenchSource, /全选/)
  assert.match(workbenchSource, /平均分配剩余数据/)
  assert.match(workbenchSource, /分配核查人/)
  assert.match(workbenchSource, /已分配 \$\{inspectorCounts\[community\]\?\.\[value\] \|\| 0\} 条/)
  assert.match(workbenchSource, /mobile-task-assignment-workbench__scroll/)
  assert.match(workbenchSource, /onPointerEnter/)
  assert.match(workbenchSource, /closable/)
  assert.doesNotMatch(workbenchSource, /if \(saving\) return/)
  assert.match(workbenchSource, /for \(const group of groups\)/)
  assert.match(workbenchSource, /setCandidates\(current => current\.filter/)
  assert.match(workbenchSource, /当前有 \$\{availableTotal\} 条未分配数据/)
  assert.match(workbenchSource, /刷新下一批/)
  assert.match(clientSource, /assignment-workbench/)
  assert.match(clientSource, /displayed_total/)
  assert.match(clientSource, /limited: boolean/)
  assert.match(clientSource, /inspector_counts_by_community/)
  const styleSource = readFileSync(
    new URL('../src/index.css', import.meta.url),
    'utf8',
  )
  assert.match(styleSource, /mobile-task-assignment-workbench__scroll[\s\S]*overflow-y: auto/)
  assert.match(styleSource, /mobile-task-assignment-workbench[\s\S]*overflow: hidden/)
})

test('唯一可靠建议作为自动匹配直接参与分配', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )
  const tableSource = readFileSync(
    new URL('../src/components/MobileTaskTable.tsx', import.meta.url),
    'utf8',
  )
  const detailSource = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
    'utf8',
  )
  const workbenchSource = readFileSync(
    new URL('../src/components/MobileTaskAssignmentWorkbench.tsx', import.meta.url),
    'utf8',
  )

  assert.match(pageSource, />小区</)
  assert.match(pageSource, /placeholder="全部小区"/)
  assert.match(pageSource, /匹配状态/)
  assert.match(tableSource, /小区归属/)
  assert.match(tableSource, /confirmMobileTaskAddressMatch/)
  assert.match(detailSource, /原始地址只读保留，不会被匹配结果覆盖/)
  assert.match(detailSource, /候选小区/)
  assert.match(workbenchSource, /status === 'confirmed' \|\| status === 'suggested'/)
  assert.match(workbenchSource, /自动匹配/)
  assert.match(workbenchSource, /“自动匹配”和“已人工确认”的任务可直接分配/)
  for (const blocked of ['ambiguous', 'conflict', 'unmatched', 'invalid']) {
    assert.match(workbenchSource, new RegExp(`${blocked}:`))
  }
})

test('本地任务详情不再把历史腾讯来源当成编辑前置条件', () => {
  const detailSource = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
    'utf8',
  )
  assert.match(detailSource, /const interactionLocked = readonlyView \|\| localSourceConflict/)
  assert.match(detailSource, /该任务存在 \$\{data\.task\.source_count\} 条本地业务来源/)
  assert.match(detailSource, /滨湖平台本地数据/)
  assert.match(detailSource, /没有可用本地任务来源/)
  assert.doesNotMatch(detailSource, /选择腾讯来源|腾讯来源行|采用腾讯值/)
})

test('任务详情桌面端使用更紧凑的最大宽度', () => {
  const styleSource = readFileSync(
    new URL('../src/index.css', import.meta.url),
    'utf8',
  )
  assert.match(styleSource, /\.mobile-task-detail-page\s*\{[\s\S]*max-width: 1240px/)
})

test('流口任务保存使用本地版本并且不再暴露腾讯冲突处理', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )
  const tableSource = readFileSync(
    new URL('../src/components/MobileTaskTable.tsx', import.meta.url),
    'utf8',
  )
  const detailSource = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
    'utf8',
  )
  const clientSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(detailSource, /expected_revision: selectedSource\.revision/)
  assert.match(detailSource, /updateMobileTask/)
  assert.doesNotMatch(detailSource, /平台与腾讯表格修改了同一字段|采用腾讯值|采用平台值/)
  assert.doesNotMatch(clientSource, /resolveMobileTaskSyncConflict|resolve-sync-conflict/)
  assert.doesNotMatch(pageSource, /腾讯写回|腾讯同步/)
  assert.doesNotMatch(tableSource, /腾讯写回|腾讯同步/)
  assert.doesNotMatch(detailSource, /已保存，滨湖平台数据已同步并写回腾讯表格/)
  assert.doesNotMatch(tableSource, /已自动保存并写回腾讯表格/)
})

test('指令核查编辑器使用防抖自动保存并提供失败重试', () => {
  const tableSource = readFileSync(
    new URL('../src/components/MobileTaskTable.tsx', import.meta.url),
    'utf8',
  )
  const detailSource = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
    'utf8',
  )
  assert.match(tableSource, /window\.setTimeout\(\(\) => \{[\s\S]*?\}, 700\)/)
  assert.match(tableSource, /保存失败[\s\S]*?重试/)
  assert.match(tableSource, /autosaveSequenceRef/)
  assert.match(detailSource, /scheduleAutoSave\(700\)/)
  assert.match(detailSource, /savingRef/)
  assert.match(detailSource, /formGenerationRef/)
  assert.match(detailSource, /task_update/)
  assert.match(detailSource, /registration_link/)
  assert.match(detailSource, /review_flow/)
  assert.match(detailSource, /expected_row_key: selectedSource\.row_key \|\| rowKey/)
  assert.doesNotMatch(detailSource, />\s*保存修改\s*</)
})

test('批量分配界面展示逐条跳过和失败原因', () => {
  const source = readFileSync(
    new URL('../src/components/MobileTaskAssignmentWorkbench.tsx', import.meta.url),
    'utf8',
  )
  assert.match(source, /本次分配结果明细/)
  assert.match(source, /失败：\{item\.row_key\} · \{item\.reason\}/)
  assert.match(source, /跳过：\{item\.row_key\} · \{item\.reason\}/)
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

test('任务快捷调照片不再要求填写原因并带入腾讯名单字段', () => {
  const detailSource = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
    'utf8',
  )
  assert.equal(detailSource.includes('photoReason'), false)
  assert.equal(detailSource.includes('请说明需要调取照片的原因'), false)
  assert.match(detailSource, /request_reason:\s*''/)
  assert.match(detailSource, /community_name:/)
  assert.match(detailSource, /source_label:\s*data\?\.workflow\.label/)
  assert.match(detailSource, /对象：/)
  assert.match(detailSource, /身份证号：/)
  assert.match(detailSource, /社区：/)
  assert.match(detailSource, /来源：/)
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
  assert.match(clientSource, /communities\.forEach\(value => params\.append\('community', value\)\)/)
  assert.match(clientSource, /communities: params\.communities \|\| \[\]/)
  assert.match(clientSource, /inspectors: params\.inspectors \|\| \[\]/)
  assert.match(pageSource, /mode="multiple"/)
  assert.match(pageSource, /getMobileTaskFilterOptions/)
  assert.match(pageSource, /getMobileTaskAnalysisFilterOptions/)
  assert.match(pageSource, /listMobileTaskAnalysis/)
  assert.match(pageSource, /全部数据/)
  assert.match(pageSource, /const listRequestId = useRef\(0\)/)
  assert.match(pageSource, /priority_counts/)
  assert.match(pageSource, /更多筛选/)
  assert.match(pageSource, /setSearchParams\(next, \{ replace: true \}\)/)
  assert.equal(pageSource.includes("next.set('keyword'"), false)
})

test('指令核查筛选区按业务顺序响应式排列并集中展示生效条件', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )
  const styleSource = readFileSync(
    new URL('../src/index.css', import.meta.url),
    'utf8',
  )

  const businessIndex = pageSource.indexOf('>业务类型</')
  const scopeIndex = pageSource.indexOf('>数据范围</')
  const searchIndex = pageSource.indexOf('>快速搜索</')
  const communityIndex = pageSource.indexOf('>社区</')
  const smallCommunityIndex = pageSource.indexOf('>小区</')
  const inspectorIndex = pageSource.indexOf('>核查人</')
  const matchStatusIndex = pageSource.indexOf('>匹配状态</')
  const secondarySource = pageSource.match(
    /<div className=\{`mobile-task-filter-secondary[\s\S]*?<div className="mobile-task-filter-controls">/,
  )?.[0] ?? ''

  assert.ok(businessIndex >= 0)
  assert.ok(scopeIndex > businessIndex)
  assert.ok(searchIndex > scopeIndex)
  assert.ok(communityIndex > searchIndex)
  assert.ok(smallCommunityIndex > communityIndex)
  assert.ok(inspectorIndex > smallCommunityIndex)
  assert.ok(matchStatusIndex > inspectorIndex)
  assert.ok((secondarySource.match(/mode="multiple"/g) || []).length >= 4)
  assert.doesNotMatch(secondarySource, /disabled=\{[^}]*communities/)
  assert.match(pageSource, /useResponsiveLayout\(pageRootRef\)/)
  assert.match(pageSource, /mobile-task-filter-card--\$\{responsiveLayout\.mode\}/)
  assert.match(styleSource, /\.mobile-task-filter-primary\s*\{[\s\S]*grid-template-columns: minmax\(170px, 0\.8fr\) minmax\(132px, 0\.55fr\) minmax\(320px, 2fr\)/)
  assert.match(styleSource, /mobile-task-filter-card--standard[\s\S]*mobile-task-filter-secondary[\s\S]*repeat\(2, minmax\(0, 1fr\)\)/)
  assert.match(styleSource, /mobile-task-filter-card--compact[\s\S]*mobile-task-filter-field--search[\s\S]*order: -1/)
  assert.match(pageSource, /mobile-task-filter-secondary\$\{analysisOnly \? ' mobile-task-filter-secondary--analysis' : ''\}/)
  assert.match(styleSource, /\.mobile-task-filter-secondary--analysis\s*\{[\s\S]*repeat\(2, minmax\(0, 1fr\)\)/)
  assert.match(pageSource, /aria-label="当前生效筛选条件"/)
  assert.match(pageSource, /activeFilterChips\.map\(chip =>/)
  assert.match(pageSource, /<Tag key=\{chip\.key\} closable onClose=\{chip\.remove\}>/)
})

test('社区变化后只移除不兼容筛选，选项加载期间保留旧值', () => {
  assert.deepEqual(
    retainAvailableMobileTaskFilters(
      ['长板社区', '湖滨社区'],
      [{ value: '湖滨社区' }, { value: '东门社区' }],
    ),
    ['湖滨社区'],
  )

  const pageSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )
  const loadOptionsSource = pageSource.match(/const loadOptions = useCallback\([\s\S]*?\n  \}, \[[^\n]+\]\)/)?.[0] ?? ''

  assert.match(loadOptionsSource, /const requestId = \+\+optionsRequestId\.current/)
  assert.match(loadOptionsSource, /if \(requestId !== optionsRequestId\.current\) return/)
  assert.match(loadOptionsSource, /retainAvailableMobileTaskFilters\(smallCommunities, result\.small_communities \|\| \[\]\)/)
  assert.match(loadOptionsSource, /retainAvailableMobileTaskFilters\(inspectors, result\.inspectors\)/)
  assert.match(loadOptionsSource, /刷新失败时保留已有选项/)
  assert.doesNotMatch(pageSource, /onChange=\{values => \{\s*setCommunities\(values\)[\s\S]*?setSmallCommunities\(\[\]\)/)
  assert.match(pageSource, /已移除 \$\{removedCount\} 个不适用于当前范围的筛选条件/)
  assert.match(pageSource, /const counts = new Map\(matchStatusOptions\.map\(option => \[option\.value, option\.count\]\)\)/)
  assert.match(pageSource, /`\$\{option\.label\}（\$\{counts\.get\(option\.value\)\}）`/)
})

test('筛选更多项、重置和导出使用同一组完整条件', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )
  const clearFiltersSource = pageSource.match(
    /const clearFilters = \(\) => \{[\s\S]*?\n  \}/,
  )?.[0] ?? ''

  assert.match(pageSource, /const advancedFilterCount = useMemo/)
  assert.match(pageSource, /if \(advancedFilterCount > 0\) setMoreOpen\(true\)/)
  assert.match(pageSource, /更多筛选'}\{advancedFilterCount \? `（\$\{advancedFilterCount\}）` : ''\}/)
  assert.match(clearFiltersSource, /setCommunities\(\[\]\)[\s\S]*setKeywordInput\(''\)[\s\S]*setMoreOpen\(false\)/)
  assert.doesNotMatch(clearFiltersSource, /setAnalysisParserSelection/)
  assert.doesNotMatch(clearFiltersSource, /updateQuery/)
  assert.match(pageSource, /exportMobileTasks\(\{[\s\S]*communities,[\s\S]*small_communities: smallCommunities,[\s\S]*match_status: matchStatuses,[\s\S]*inspectors,[\s\S]*watch_categories: watchCategories,[\s\S]*priority,[\s\S]*sort,[\s\S]*keyword:/)
  assert.match(pageSource, /exportMobileTaskAnalysis\(\{[\s\S]*parser_types: analysisParserTypes,[\s\S]*review_stage: reviewStage,[\s\S]*communities,[\s\S]*inspectors,[\s\S]*watch_categories: watchCategories,[\s\S]*sort,[\s\S]*keyword:/)
  assert.match(pageSource, /onPressEnter=\{\(\) => setKeywordFlush\(current => current \+ 1\)\}/)
  assert.match(pageSource, /useDebouncedValue\(keywordInput\.trim\(\), 350, keywordFlush\)/)
})

test('全所范围为只读说明，普通用户仍可切换我的和社区', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )

  assert.match(pageSource, /aria-label="数据范围：全所"/)
  assert.match(pageSource, />\s*全所数据\s*</)
  assert.match(pageSource, /<Segmented[\s\S]*label: '我的'[\s\S]*label: '社区'/)
  assert.doesNotMatch(pageSource, /<Button[^>]*>全所<\/Button>/)
})

test('流口任务支持按地址或身份证号对完整结果排序', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )
  const clientSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )
  const tableSource = readFileSync(
    new URL('../src/components/MobileTaskTable.tsx', import.meta.url),
    'utf8',
  )

  assert.match(pageSource, /默认（状态 \+ 地址）/)
  assert.match(pageSource, /地址升序[\s\S]*?address_asc/)
  assert.match(pageSource, /身份证号升序[\s\S]*?identity_asc/)
  assert.match(pageSource, />排序方式</)
  assert.match(pageSource, /options=\{SORT_OPTIONS\}/)
  assert.match(pageSource, /sort=\{sort\}/)
  assert.match(pageSource, /onSortChange=\{setSort\}/)
  assert.match(tableSource, /title: '身份证号码'[\s\S]*?sorter: true[\s\S]*?sort === 'identity_asc'/)
  assert.match(tableSource, /title: '地址'[\s\S]*?sorter: true[\s\S]*?sort === 'address_asc'/)
  assert.match(tableSource, /activeSorter\.columnKey === 'identity_number'[\s\S]*?onSortChange\('identity_asc'\)/)
  assert.match(tableSource, /activeSorter\.columnKey === 'address'[\s\S]*?onSortChange\('address_asc'\)/)
  assert.match(clientSource, /MobileTaskSort =[\s\S]*?'address_asc'[\s\S]*?'identity_asc'/)
  assert.match(clientSource, /sort: params\.sort \|\| 'priority'/)
})

test('流口任务数量卡按全部、普通待处理、等待研判、已研判、来源异常、已完成排列', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )
  const allIndex = pageSource.indexOf("{ key: 'all'")
  const ordinaryIndex = pageSource.indexOf("{ key: 'ordinary'")
  const waitingIndex = pageSource.indexOf("{ key: 'waiting_analysis'")
  const analyzedIndex = pageSource.indexOf("{ key: 'analyzed'")
  const exceptionIndex = pageSource.indexOf("{ key: 'source_exception'")
  const completedIndex = pageSource.indexOf("{ key: 'completed'")
  assert.ok(allIndex >= 0)
  assert.ok(ordinaryIndex > allIndex)
  assert.ok(waitingIndex > ordinaryIndex)
  assert.ok(analyzedIndex > waitingIndex)
  assert.ok(exceptionIndex > analyzedIndex)
  assert.ok(completedIndex > exceptionIndex)
  assert.match(pageSource, /setReviewStage\('all'\)/)
})

test('流口任务不再提供待同步筛选入口，但保留任务级同步状态提示', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )
  const styles = readFileSync(
    new URL('../src/index.css', import.meta.url),
    'utf8',
  )
  const filterOptions = pageSource.match(/const PRIORITY_OPTIONS = \[[\s\S]*?\n\]/)?.[0] ?? ''
  const priorityCards = pageSource.match(/const PRIORITY_CARDS:[\s\S]*?\n\]/)?.[0] ?? ''

  assert.doesNotMatch(filterOptions, /pending_sync|待同步/)
  assert.doesNotMatch(priorityCards, /pending_sync|待同步/)
  assert.match(pageSource, /task\.sync_state === 'pending'/)
  assert.match(pageSource, />待同步<\/Tag>/)
  assert.match(styles, /\.mobile-task-priority-grid\s*\{[\s\S]*?repeat\(6,/)
  assert.doesNotMatch(styles, /\.mobile-task-priority-grid\s*\{[\s\S]*?repeat\(7,/)
})

test('普通选择字段在手机端关闭搜索，房屋关联仍允许模糊查找', () => {
  const detailSource = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
    'utf8',
  )
  assert.match(detailSource, /const mobile = useMobileViewport\(\)/)
  assert.match(detailSource, /showSearch=\{!mobile\}/)
  assert.match(detailSource, /placeholder="搜索并选择辖区档案中的唯一房屋"/)
  assert.match(detailSource, /onSearch=\{value => void loadRegistrationProperties\(value\)\}/)
})

test('全民防仅保留反馈状态只读查询，不再提供真实登记入口', () => {
  const detailSource = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
    'utf8',
  )
  const clientSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )
  assert.doesNotMatch(detailSource, /data\.qmf_preview\?\.visible/)
  assert.doesNotMatch(detailSource, /previewQmfRegistration/)
  assert.doesNotMatch(detailSource, /全民防只读预演/)
  assert.match(detailSource, /data\.qmf_registration\?\.visible/)
  assert.match(detailSource, /getQmfLegacyStatus/)
  assert.match(detailSource, /平台仅展示只读核对结果，不会向全民防写入核查结果/)
  assert.doesNotMatch(detailSource, /prepareQmfRegistration|executeQmfRegistration|getQmfRegistrationRun/)
  assert.doesNotMatch(detailSource, /全民防模型三登记确认|二次确认并执行全民防登记|重新核对并准备/)
  assert.doesNotMatch(detailSource, /批量登记|自动登记|定时登记/)
  assert.match(clientSource, /export async function getQmfLegacyStatus/)
  assert.doesNotMatch(clientSource, /export async function (prepare|execute)QmfRegistration/)
  assert.doesNotMatch(clientSource, /retryQmfTencentMarker/)
})
