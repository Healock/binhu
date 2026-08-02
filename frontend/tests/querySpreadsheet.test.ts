import assert from 'node:assert/strict'
import test from 'node:test'
import { BorderStyleTypes } from '@univerjs/core'
import {
  applyQuerySheetValues,
  buildQuerySheetRows,
  canEditQuerySheetCell,
  isQuerySheetRangeEditable,
  parseQuerySheetClipboard,
  resolveQuerySheetThinBorderStyle,
  selectedQuerySheetRow,
  updateQuerySheetDrafts,
} from '../src/utils/querySpreadsheet.ts'

const columns = ['社区', '核查人', '姓名']

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
