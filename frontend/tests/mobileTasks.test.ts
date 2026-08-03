import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildMobileTaskChanges,
  mobileTaskEditorFields,
  mobileTaskCanLaunchTelephone,
  mobileTaskPhoneOptions,
  mobileTaskPhoneValue,
  mobileTaskSourceDifferences,
  mobileTaskSourceNeedsReview,
  mobileTaskSourceState,
  sortMobileTaskBusinesses,
} from '../src/utils/mobileTasks.ts'
import {
  isFlowTaskPosition,
  shouldUseMobileTaskWorkbench,
} from '../src/utils/mobileTaskRouting.ts'
import {
  mobileNavigationItemLabel,
  navigationItemById,
  routeIsActive,
} from '../src/navigation/mobileNavigation.ts'

test('只有组员和组长在手机端切换到任务工作台', () => {
  assert.equal(isFlowTaskPosition('组员'), true)
  assert.equal(isFlowTaskPosition('组长'), true)
  assert.equal(shouldUseMobileTaskWorkbench('组员', true), true)
  assert.equal(shouldUseMobileTaskWorkbench('组员', false), false)
  assert.equal(shouldUseMobileTaskWorkbench('基础管控', true), false)
})

test('流口岗手机导航复用旧配置 ID 并显示新名称', () => {
  const summary = navigationItemById('online_summary')!
  const query = navigationItemById('online_query')!
  assert.equal(mobileNavigationItemLabel(summary, '组员', true), '首页')
  assert.equal(mobileNavigationItemLabel(query, '组长'), '任务处理')
  assert.equal(mobileNavigationItemLabel(query, '基础管控'), '在线数据查询')
  assert.equal(routeIsActive('/tasks/全链条/row', query), true)
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

test('只有手机浏览器会直接启动 tel 协议', () => {
  assert.equal(mobileTaskCanLaunchTelephone('Mozilla/5.0 (Linux; Android 15)'), true)
  assert.equal(mobileTaskCanLaunchTelephone('Mozilla/5.0 (Windows NT 10.0)'), false)
  assert.equal(mobileTaskCanLaunchTelephone('Mozilla/5.0 (Macintosh)', false, 5), true)
})
