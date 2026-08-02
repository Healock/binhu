import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildQueryDisplayRows,
  canEditQueryCell,
  createQueryDraftRow,
  ensureTrailingQueryDraft,
  isQueryDraftTouched,
  missingQueryDraftFields,
  normalizeQueryResponse,
  saveChangedSourceFields,
} from '../src/utils/queryGrid.ts'

const sourceRow = {
  id: 9,
  physical_row: 23,
  values: { 社区: '长板', 核查结果: '无法核实', 二次反馈: '' },
  cell_meta: {},
  revision: 4,
  row_hash: 'hash',
  editable_fields: ['核查结果', '二次反馈'],
  can_delete: false,
}

test('查询响应缺省集合会被标准化', () => {
  const result = normalizeQueryResponse({
    data: undefined as never,
    columns: undefined as never,
    column_meta: undefined as never,
    total: undefined as never,
    page: 0,
    page_size: 0,
    source_ready: 0 as never,
    writeback_enabled: 1 as never,
    can_add: 0 as never,
    required_fields: undefined as never,
    pending_count: undefined as never,
  })

  assert.deepEqual(result.data, [])
  assert.deepEqual(result.columns, [])
  assert.equal(result.total, 0)
  assert.equal(result.page, 1)
  assert.equal(result.writeback_enabled, true)
  assert.deepEqual(result.required_fields, [])
})

test('行内新增始终保留一条尾部空行', () => {
  const columns = ['社区', '身份证号', '核查结果']
  let sequence = 0
  const createId = () => `draft-${++sequence}`
  const initial = ensureTrailingQueryDraft([], columns, createId)

  assert.equal(initial.length, 1)
  assert.equal(isQueryDraftTouched(initial[0], columns), false)

  const filled = { ...initial[0], 社区: '长板' }
  const next = ensureTrailingQueryDraft([filled], columns, createId)
  assert.equal(next.length, 2)
  assert.equal(next[0].社区, '长板')
  assert.equal(isQueryDraftTouched(next[1], columns), false)

  const withExtraBlanks = ensureTrailingQueryDraft(
    [...next, createQueryDraftRow(columns, 'extra')],
    columns,
    createId,
  )
  assert.equal(withExtraBlanks.length, 2)
})

test('行内新增只在必填字段完整后允许提交', () => {
  const row = createQueryDraftRow(['社区', '身份证号', '核查结果'], 'draft')
  row.社区 = '长板'

  assert.deepEqual(
    missingQueryDraftFields(row, ['社区', '身份证号']),
    ['身份证号'],
  )
  assert.equal(canEditQueryCell('online', row, '身份证号', true), true)
  assert.equal(canEditQueryCell('archive', row, '身份证号', true), false)
})

test('重复父行展开后保留父子顺序和腾讯物理行号', () => {
  const rows = [{ __row_key: 'parent', __source_count: 2, 社区: '长板' }]
  const display = buildQueryDisplayRows(rows, { parent: [sourceRow] })

  assert.equal(display.length, 2)
  assert.equal(display[0].__kind, 'parent')
  assert.equal(display[1].__kind, 'source')
  assert.equal(display[1].__physical_row, 23)
})

test('只有当前数据的来源子行和授权字段可编辑', () => {
  const child = buildQueryDisplayRows(
    [{ __row_key: 'parent', __source_count: 2 }],
    { parent: [sourceRow] },
  )[1]

  assert.equal(canEditQueryCell('online', child, '核查结果'), true)
  assert.equal(canEditQueryCell('online', child, '研判'), false)
  assert.equal(canEditQueryCell('archive', child, '核查结果'), false)
})

test('手机抽屉连续保存会把上一格的新版本传给下一格', async () => {
  const received: Array<[string, number]> = []
  await saveChangedSourceFields(
    sourceRow,
    { ...sourceRow.values, 核查结果: '无法核实-已联系', 二次反馈: '再次上门' },
    async (column, _value, revision) => {
      received.push([column, revision])
      return { revision: revision + 1 }
    },
  )

  assert.deepEqual(received, [
    ['核查结果', 4],
    ['二次反馈', 5],
  ])
})
