import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { BorderStyleTypes, CellValueType } from '@univerjs/core'
import {
  applyQuerySheetValues,
  buildQuerySheetRequestFilters,
  buildQuerySheetRows,
  canEditQuerySheetCell,
  fitQuerySheetColumnWidth,
  isQuerySheetFullscreen,
  isQuerySheetAutomaticTextConversion,
  isQuerySheetRangeEditable,
  parseQuerySheetClipboard,
  QUERY_SHEET_FEATURE_CONFIG,
  QUERY_SHEET_UI_CONFIG,
  querySheetPalette,
  querySheetTextCell,
  querySheetCellKey,
  resolveQuerySheetColumnWidth,
  resolveQuerySheetThinBorderStyle,
  selectedQuerySheetRow,
  toggleQuerySheetFullscreen,
  updateQuerySheetDrafts,
} from '../src/utils/querySpreadsheet.ts'

test('automatic spreadsheet coercion is detected and blocked', () => {
  assert.equal(
    isQuerySheetAutomaticTextConversion('身份证号', '32052519911016025X', '320525199110160260'),
    true,
  )
  assert.equal(
    isQuerySheetAutomaticTextConversion('手机号', '1380013800013900138000', '1380013800013900137984'),
    true,
  )
  assert.equal(isQuerySheetAutomaticTextConversion('日期', '7.30', '7.3'), true)
  assert.equal(
    isQuerySheetAutomaticTextConversion('身份证号', '32052519911016025X', '32052519911016025X'),
    false,
  )
})

test('spreadsheet reconciliation only applies explicitly edited cells', () => {
  const rows = buildQuerySheetRows([{
    [columns[0]]: 'community-before',
    [columns[1]]: 'inspector-before',
    [columns[2]]: 'name-before',
    __row_key: 'one',
    __source_count: 1,
    __source_id: 12,
    __revision: 3,
    __editable_fields: [columns[0], columns[1]],
  }], [], columns, false, index => `blank-${index}`)
  const changes = applyQuerySheetValues(
    rows,
    columns,
    [['community-after', 'inspector-auto', 'name-auto']],
    new Set([querySheetCellKey(1, 0)]),
  )
  assert.deepEqual(changes.map(change => [change.column, change.before, change.after]), [
    [columns[0], 'community-before', 'community-after'],
  ])
})

test('spreadsheet reconciliation reads edited cells instead of the whole sheet', () => {
  const componentSource = readFileSync(
    new URL('../src/components/QuerySpreadsheet.tsx', import.meta.url),
    'utf8',
  )
  assert.doesNotMatch(componentSource, /dataRange\.getValues\(\)/)
  assert.match(componentSource, /cell\.getValue\(\)/)
})

const columns = ['社区', '核查人', '姓名']

test('查询工作表关闭长数字文本误报并由 Univer 统一转换深浅色', () => {
  assert.equal(QUERY_SHEET_FEATURE_CONFIG.disableForceStringAlert, true)
  assert.equal(QUERY_SHEET_FEATURE_CONFIG.disableForceStringMark, true)
  assert.equal(querySheetPalette(false).background, '#ffffff')
  assert.deepEqual(querySheetPalette(true), querySheetPalette(false))
})

test('腾讯日期和长数字以普通字符串写入工作表且不显示前导单引号', () => {
  assert.deepEqual(querySheetTextCell('7.30'), {
    v: '7.30',
    t: CellValueType.STRING,
  })
  assert.deepEqual(querySheetTextCell(320525199110160250n), {
    v: '320525199110160250',
    t: CellValueType.STRING,
  })
})

test('查询工作表支持 Univer 内部复制内容回退粘贴', () => {
  const componentSource = readFileSync(
    new URL('../src/components/QuerySpreadsheet.tsx', import.meta.url),
    'utf8',
  )
  assert.match(componentSource, /BeforeClipboardChange/)
  assert.match(componentSource, /internalClipboard/)
  assert.match(componentSource, /fromRange\?\.getValues/)
  assert.match(componentSource, /const pasted = clipboardValues\(params\) \|\| internalClipboard/)
})

test('查询工作表编辑日期时保留原始文本和尾零', () => {
  const componentSource = readFileSync(
    new URL('../src/components/QuerySpreadsheet.tsx', import.meta.url),
    'utf8',
  )
  assert.match(componentSource, /SheetEditChanging/)
  assert.match(componentSource, /editingValues/)
  assert.match(componentSource, /日期\|时间/)
})

test('工作表点击和拖动同时锁定主内容区与浏览器文档位置', () => {
  const componentSource = readFileSync(
    new URL('../src/components/QuerySpreadsheet.tsx', import.meta.url),
    'utf8',
  )
  assert.match(componentSource, /document\.scrollingElement/)
  assert.match(componentSource, /documentScroller\.scrollTop/)
  assert.match(componentSource, /window\.addEventListener\('scroll', restorePagePosition\)/)
  assert.match(componentSource, /container\.addEventListener\('pointerdown', handlePointerDown, true\)/)
  assert.doesNotMatch(componentSource, /isQuerySheetHorizontalScrollbarPointer/)
  assert.match(componentSource, /}, 420\)/)
})

test('单元格保存只重绘受影响行且不重新查询整张工作表', () => {
  const componentSource = readFileSync(
    new URL('../src/components/QuerySpreadsheet.tsx', import.meta.url),
    'utf8',
  )
  const pageSource = readFileSync(
    new URL('../src/pages/DataQuery.tsx', import.meta.url),
    'utf8',
  )
  assert.match(componentSource, /changedRows\.forEach\(rowIndex => applyRowAppearance/)
  assert.doesNotMatch(pageSource, /setRows\(current => \[\.\.\.current\]\)/)
  assert.doesNotMatch(pageSource, /if \(keyword \|\| Object\.keys\(sheetFilterCriteria\)\.length > 0\) await fetchData\(\)/)
})

test('工作表自动列宽保留合理的最小值和最大值', () => {
  assert.equal(fitQuerySheetColumnWidth('下发日期', 40), 92)
  assert.equal(fitQuerySheetColumnWidth('姓名', 96), 114)
  assert.equal(fitQuerySheetColumnWidth('身份证号', 500), 200)
  assert.equal(fitQuerySheetColumnWidth('地址', 900), 280)
  assert.equal(resolveQuerySheetColumnWidth('下发日期', ['7.30', '7.31']), 92)
  assert.equal(resolveQuerySheetColumnWidth('身份证号', ['32052519911016025X']), 176)
  assert.equal(resolveQuerySheetColumnWidth('地址', ['吴江松陵镇开平路2188号吾悦商业广场']), 260)
})

test('查询工作表同时启用标题区域和经典工具栏', () => {
  // Univer 0.25.x 的工具栏受 header && toolbar 共同控制。
  assert.equal(QUERY_SHEET_UI_CONFIG.header, true)
  assert.equal(QUERY_SHEET_UI_CONFIG.toolbar, true)
  assert.equal(QUERY_SHEET_UI_CONFIG.ribbonType, 'classic')
})

test('查询工作表使用文档根节点全屏以保留工具栏和筛选浮层', async () => {
  let requested = 0
  let exited = 0
  const documentRoot = {
    requestFullscreen: async () => { requested += 1 },
  } as unknown as HTMLElement
  const sheetCard = {} as HTMLElement

  assert.equal(isQuerySheetFullscreen(sheetCard, documentRoot), false)
  assert.equal(isQuerySheetFullscreen(documentRoot, documentRoot), true)

  await toggleQuerySheetFullscreen(documentRoot, null, async () => { exited += 1 })
  assert.equal(requested, 1)
  assert.equal(exited, 0)

  await toggleQuerySheetFullscreen(documentRoot, documentRoot, async () => { exited += 1 })
  assert.equal(requested, 1)
  assert.equal(exited, 1)
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
