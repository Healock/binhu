import assert from 'node:assert/strict'
import test from 'node:test'
import { BorderStyleTypes } from '@univerjs/core'
import {
  applyQuerySheetValues,
  buildQuerySheetRequestFilters,
  buildQuerySheetRows,
  canEditQuerySheetCell,
  isQuerySheetRangeEditable,
  parseQuerySheetClipboard,
  QUERY_SHEET_FEATURE_CONFIG,
  QUERY_SHEET_UI_CONFIG,
  querySheetPalette,
  resolveQuerySheetThinBorderStyle,
  selectedQuerySheetRow,
  updateQuerySheetDrafts,
} from '../src/utils/querySpreadsheet.ts'

const columns = ['社区', '核查人', '姓名']

test('查询工作表关闭长数字文本误报并由 Univer 统一转换深浅色', () => {
  assert.equal(QUERY_SHEET_FEATURE_CONFIG.disableForceStringAlert, true)
  assert.equal(QUERY_SHEET_FEATURE_CONFIG.disableForceStringMark, true)
  assert.equal(querySheetPalette(false).background, '#ffffff')
  assert.deepEqual(querySheetPalette(true), querySheetPalette(false))
})

test('查询工作表同时启用标题区域和经典工具栏', () => {
  // Univer 0.25.x 的工具栏受 header && toolbar 共同控制。
  assert.equal(QUERY_SHEET_UI_CONFIG.header, true)
  assert.equal(QUERY_SHEET_UI_CONFIG.toolbar, true)
  assert.equal(QUERY_SHEET_UI_CONFIG.ribbonType, 'classic')
})

test('工作表值筛选和条件筛选转换为完整查询请求', () => {
  const result = buildQuerySheetRequestFilters({
    社区: {
      colId: 0,
      filters: { blank: true, filters: ['长板', '冬梅', '长板'] },
    },
    姓名: {
      colId: 2,
      customFilters: { customFilters: [{ val: '*张*' }] },
    },
  })

  assert.deepEqual(result.filters, { 社区: ['长板', '冬梅', ''] })
  assert.deepEqual(result.gridFilters, {
    姓名: { type: 'contains', filter: '张' },
  })
  assert.deepEqual(result.unsupportedColorColumns, [])
})

test('工作表复合条件、非空和颜色筛选转换正确', () => {
  const result = buildQuerySheetRequestFilters({
    数量: {
      colId: 0,
      customFilters: {
        and: 1,
        customFilters: [
          { val: 10, operator: 'greaterThanOrEqual' },
          { val: 20, operator: 'lessThanOrEqual' },
        ],
      },
    },
    地址: {
      colId: 1,
      customFilters: { customFilters: [{ val: '*园', operator: 'notEqual' }] },
    },
    结果: {
      colId: 2,
      customFilters: { customFilters: [{ val: '', operator: 'notEqual' }] },
    },
    状态: {
      colId: 3,
      colorFilters: { cellFillColors: ['#ffffff'] },
    },
  })

  assert.deepEqual(result.gridFilters, {
    数量: {
      operator: 'and',
      conditions: [
        { type: 'greaterThanOrEqual', filter: '10' },
        { type: 'lessThanOrEqual', filter: '20' },
      ],
    },
    地址: { type: 'notEndsWith', filter: '园' },
    结果: { type: 'notBlank', filter: '' },
  })
  assert.deepEqual(result.unsupportedColorColumns, ['状态'])
})

test('使用 Univer 运行时实际提供的细边框枚举', () => {
  assert.equal(
    resolveQuerySheetThinBorderStyle({ BorderStyleTypes }),
    BorderStyleTypes.THIN,
  )
  assert.equal(resolveQuerySheetThinBorderStyle({}), null)
})

test('工作表表头、重复父行和归档数据保持只读', () => {
  const rows = buildQuerySheetRows([
    {
      社区: '长板',
      核查人: '张三',
      姓名: '测试',
      __row_key: 'one',
      __source_count: 2,
      __editable_fields: ['社区', '核查人'],
    },
  ], [], columns, false, index => `blank-${index}`)

  assert.equal(isQuerySheetRangeEditable('online', rows, columns, 0, 0, 1, 1, false), false)
  assert.equal(canEditQuerySheetCell('online', rows[0], '社区', false), false)
  assert.equal(canEditQuerySheetCell('archive', rows[0], '社区', false), false)
})

test('单来源行只允许后端声明的字段', () => {
  const rows = buildQuerySheetRows([
    {
      社区: '长板',
      核查人: '张三',
      姓名: '测试',
      __row_key: 'one',
      __source_count: 1,
      __source_id: 12,
      __revision: 3,
      __editable_fields: ['社区', '核查人'],
    },
  ], [], columns, false, index => `blank-${index}`)

  assert.equal(canEditQuerySheetCell('online', rows[0], '社区', false), true)
  assert.equal(canEditQuerySheetCell('online', rows[0], '姓名', false), false)
})

test('新增权限把自然空白行变成连续草稿', () => {
  const rows = buildQuerySheetRows([], [], columns, true, index => `blank-${index}`, 2)
  assert.equal(rows.length, 2)
  assert.equal(canEditQuerySheetCell('online', rows[0], '姓名', true), true)

  const changes = applyQuerySheetValues(rows, columns, [
    ['长板', '张三', '甲'],
    ['', '', ''],
  ])
  assert.equal(changes.length, 3)
  assert.equal(rows[0].kind, 'draft')
  assert.deepEqual(updateQuerySheetDrafts(rows, columns).map(row => row.姓名), ['甲'])
  assert.equal(selectedQuerySheetRow(rows, 1)?.姓名, '甲')
})

test('粘贴矩阵只在全部目标可编辑时通过', () => {
  const rows = buildQuerySheetRows([
    {
      社区: '长板',
      核查人: '张三',
      姓名: '测试',
      __row_key: 'one',
      __source_count: 1,
      __source_id: 12,
      __revision: 3,
      __editable_fields: ['社区', '核查人'],
    },
  ], [], columns, false, index => `blank-${index}`)

  assert.deepEqual(parseQuerySheetClipboard('冬梅\t李四\r\n'), [['冬梅', '李四']])
  assert.equal(isQuerySheetRangeEditable('online', rows, columns, 1, 0, 1, 2, false), true)
  assert.equal(isQuerySheetRangeEditable('online', rows, columns, 1, 0, 1, 3, false), false)
})
