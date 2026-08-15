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

test('全链条新增待登记结果保留为正式任务选项', () => {
  assert.equal(
    mobileTaskSourceState('全链条', '核查结果', { 核查结果: '待登记' }),
    'completed',
  )
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

test('已完成快捷调照片结果在任务详情可预览和下载', () => {
  const detailSource = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
    'utf8',
  )
  const clientSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(detailSource, /已调取照片/)
  assert.match(detailSource, /workflowApi\.attachmentUrl\(request\.ticket_id, attachment\.file_id, true\)/)
  assert.match(detailSource, /<DownloadOutlined/)
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
  assert.match(pageSource, /currentAddress \? '现住址' : '地址'/)
  assert.match(pageSource, /mobile-task-item-card__key-row--old-address/)
  assert.match(pageSource, /<dt>原地址<\/dt>/)
  assert.match(pageSource, /mobile-task-item-card__flags/)
  assert.match(pageSource, /task\.photo_fetched/)
  assert.match(pageSource, /<Tag color="green">已调照片<\/Tag>/)
  assert.ok(pageSource.indexOf('已研判') < pageSource.indexOf('已调照片'))
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
  assert.match(pageSource, /const \[selectionMode, setSelectionMode\] = useState\(false\)/)
  assert.match(pageSource, /\{selectionMode \? '退出选择' : '选择'\}/)
  assert.match(pageSource, /onClick=\{openOrSelectTask\}/)
  assert.match(pageSource, /bulkMode/)
  assert.match(pageSource, /平均分配/)
  assert.match(pageSource, /assignment_counts/)
  assert.match(pageSource, /selectMobileTasksForAssignment/)
  assert.match(pageSource, /全选当前筛选/)
  assert.match(pageSource, /row_keys: chunk/)
  assert.match(pageSource, /MOBILE_TASK_ASSIGNMENT_CHUNK_SIZE/)
  assert.match(pageSource, /for \(let offset = processed; offset < rowKeys\.length;/)
  assert.match(pageSource, /balanced_offset: bulkMode === 'balanced' \? offset/)
  assert.match(pageSource, /balanced_total: bulkMode === 'balanced' \? rowKeys\.length/)
  assert.match(pageSource, /点击“继续分配”会从当前分块续传/)
  assert.match(pageSource, /<Progress/)
  assert.match(pageSource, /跳过原因：/)
  assert.match(pageSource, /result\.failed_details/)
  assert.match(pageSource, /失败原因：/)
  assert.match(pageSource, /setInterval\(refreshVisibleList, 30_000\)/)
  assert.match(pageSource, /visibilitychange/)
  assert.doesNotMatch(pageSource, /selectAllLoaded/)
  assert.match(pageSource, /formatMobileTaskDeadline/)
  assert.match(pageSource, /aria-pressed=\{selectionMode \? isSelected : undefined\}/)
  assert.match(pageSource, /isSelected \? 'is-selected'/)
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
  assert.match(styleSource, /\.mobile-task-item-card\.is-selected[\s\S]*box-shadow:/)
  assert.match(styleSource, /\.mobile-task-bulk-toolbar\.is-sticky[\s\S]*position:\s*sticky/)
  assert.match(styleSource, /\.mobile-task-balanced-preview/)
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
  const styleSource = readFileSync(
    new URL('../src/index.css', import.meta.url),
    'utf8',
  )
  const settingsSource = readFileSync(
    new URL('../src/pages/PersonalizationSettings.tsx', import.meta.url),
    'utf8',
  )

  assert.match(pageSource, /user\?\.task_display_mode \|\| 'card'/)
  assert.match(pageSource, /<MobileTaskTable/)
  assert.match(pageSource, /className="hidden md:block"/)
  assert.match(pageSource, /taskDisplayMode === 'table' \? ' mobile-task-list--table-fallback' : ''/)
  assert.match(styleSource, /@media \(min-width: 768px\)[\s\S]*mobile-task-list\.mobile-task-list--table-fallback[\s\S]*display: none/)
  assert.equal(pageSource.includes("taskDisplayMode === 'table' ? ' md:hidden'"), false)
  assert.match(pageSource, /taskDisplayMode === 'table'[\s\S]*requestPage\(loadedPageRef\.current\)/)
  assert.match(tableSource, /Table<MobileTaskItem>/)
  assert.match(tableSource, /title: '截止日期'/)
  assert.match(tableSource, /title: '登记情况'/)
  assert.match(tableSource, /expandedRowRender/)
  assert.match(tableSource, /mobile-task-table-primary-row/)
  assert.doesNotMatch(tableSource, /编辑本行/)
  assert.match(tableSource, /getMobileTaskInlineEditors/)
  assert.match(tableSource, /rows\.map\(task => task\.row_key\)/)
  assert.match(tableSource, /updateMobileTask/)
  assert.match(tableSource, /const visiblePhones = phones\.slice\(0, 3\)/)
  assert.match(tableSource, /phones\.length - visiblePhones\.length/)
  assert.match(tableSource, /const saveField = async/)
  assert.match(tableSource, /onBlur=\{\(\) => void saveField\(/)
  assert.doesNotMatch(tableSource, /保存 \$\{dirtyCount\} 项/)
  assert.doesNotMatch(tableSource, /title="查看任务"/)
  assert.match(tableSource, /<Tooltip title=\{task\.summary\.analysis \|\| '未填写'\}>/)
  assert.match(tableSource, /<Tooltip title=\{task\.summary\.analysis\}>/)
  assert.match(tableSource, /mobileTaskEditorFields/)
  assert.match(tableSource, /placeholder="请选择"/)
  assert.match(tableSource, /placeholder="请输入"/)
  assert.match(tableSource, />现住址</)
  assert.match(tableSource, />核查结果</)
  assert.match(tableSource, />研判</)
  assert.match(tableSource, />二次反馈</)
  assert.match(tableSource, />调取照片</)
  assert.match(tableSource, /hideSelectAll: true/)
  assert.match(tableSource, /current: page/)
  assert.match(tableSource, /pageSize: 50/)
  assert.match(tableSource, /mobile-task-source-cloud mobile-task-source-cloud--table/)
  assert.match(tableSource, /mobileTaskSourceTags\(task\.summary\.source\)[\s\S]*sources\.map\(tag =>/)
  assert.match(styleSource, /mobile-task-source-cloud--table[\s\S]*margin-top: 0/)
  assert.match(styleSource, /\.mobile-task-table\s*\{[\s\S]*padding: 0 10px 10px/)
  assert.match(styleSource, /mobile-task-table-primary-row > td:first-child[\s\S]*border-start-start-radius: 12px/)
  assert.match(styleSource, /mobile-task-table-primary-row > td:last-child[\s\S]*border-start-end-radius: 12px/)
  assert.match(styleSource, /ant-table-expanded-row > td[\s\S]*padding: 0 0 10px/)
  assert.match(styleSource, /ant-table-expanded-row-fixed[\s\S]*width: 100% !important[\s\S]*margin: 0 !important[\s\S]*padding: 0 !important/)
  assert.match(styleSource, /mobile-task-table-inline-editor\s*\{[\s\S]*border-radius: 0 0 12px 12px/)
  assert.match(styleSource, /mobile-task-table-inline-editor\s*\{[\s\S]*box-shadow: 0 5px 14px/)
  assert.doesNotMatch(styleSource, /inset 3px 0 0 color-mix/)
  assert.match(styleSource, /mobile-task-table-inline-fields/)
  assert.match(styleSource, /mobile-task-table-inline-editor--dirty/)
  assert.match(styleSource, /mobile-task-table-row-selected \+ \.ant-table-expanded-row[\s\S]*border-color: var\(--app-primary\)/)
  assert.match(settingsSource, /流口任务展示/)
  assert.match(settingsSource, /task_display_mode: taskDisplayMode/)
  assert.match(settingsSource, /卡片视图/)
  assert.match(settingsSource, /表格视图/)
})

test('任务详情桌面端使用更紧凑的最大宽度', () => {
  const styleSource = readFileSync(
    new URL('../src/index.css', import.meta.url),
    'utf8',
  )
  assert.match(styleSource, /\.mobile-task-detail-page\s*\{[\s\S]*max-width: 1240px/)
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
  assert.match(pageSource, /getMobileTaskFilterOptions\([\s\S]*parserType,[\s\S]*scope,[\s\S]*communities,[\s\S]*analysisOnly \? reviewStage : 'all'/)
  assert.match(pageSource, /const listRequestId = useRef\(0\)/)
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

test('mobile task choice fields disable search on mobile', () => {
  const detailSource = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
    'utf8',
  )
  assert.match(detailSource, /const mobile = useMobileViewport\(\)/)
  assert.match(detailSource, /showSearch=\{!mobile\}/)
  assert.equal(detailSource.includes('\n                        showSearch\n'), false)
})

test('全民防预演只从任务详情按内部来源定位发起且没有提交入口', () => {
  const detailSource = readFileSync(
    new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url),
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

  assert.match(detailSource, /data\.qmf_preview\?\.visible/)
  assert.match(detailSource, /data\.qmf_preview\.enabled/)
  assert.match(detailSource, /expected_revision: selectedSource\.revision/)
  assert.match(detailSource, /qmfPreviewRequestActive\.current/)
  assert.match(detailSource, /disabled=\{!data\.qmf_preview\.enabled \|\| dirty \|\| qmfPreviewLoading\}/)
  assert.match(detailSource, /仅供人工核对，不会执行登记或反馈/)
  assert.match(detailSource, /qmfPreviewResult\.photo\.data_base64/)
  assert.match(detailSource, /后续登记步骤/)
  assert.match(detailSource, /<Tag>未开放<\/Tag>/)
  assert.doesNotMatch(detailSource, /全民防模型三只读预演[\s\S]*提交登记/)

  const apiFunction = clientSource.match(
    /export async function previewQmfRegistration[\s\S]*?\n}\n/,
  )?.[0] || ''
  assert.match(apiFunction, /api\.post\('\/qmf-registration\/preview', payload/)
  assert.match(apiFunction, /parser_type: string/)
  assert.match(apiFunction, /row_key: string/)
  assert.match(apiFunction, /source_id: number/)
  assert.match(apiFunction, /expected_revision: number/)
  assert.doesNotMatch(apiFunction, /identity_number/)
  assert.match(styleSource, /\.qmf-preview-person[\s\S]*grid-template-columns:/)
  assert.match(styleSource, /@media \(max-width: 767px\)[\s\S]*\.qmf-preview-person[\s\S]*grid-template-columns: minmax\(0, 1fr\)/)
})
