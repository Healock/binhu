import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('列表工具栏和系统设置页面使用明确的间距布局', () => {
  const workflowSource = readFileSync(
    new URL('../src/pages/WorkflowTickets.tsx', import.meta.url),
    'utf8',
  )
  const settingsSource = readFileSync(
    new URL('../src/pages/SystemSettings.tsx', import.meta.url),
    'utf8',
  )
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')

  assert.match(workflowSource, /<ListToolbar/)
  assert.match(workflowSource, /notice={<Alert/)
  assert.match(workflowSource, /meta={<>/)
  assert.match(workflowSource, /workflow-ticket-detail__section/)
  assert.match(styles, /\.workflow-ticket-detail\s*\{[^}]*gap:\s*24px/s)
  assert.match(styles, /\.list-toolbar\s*\{[^}]*gap:\s*12px/s)
  assert.match(styles, /@media \(max-width: 767px\)[\s\S]*?\.list-toolbar__filters[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\)/)

  assert.match(settingsSource, /settings-field--counted/)
  assert.match(settingsSource, /settings-field__hint/)
  assert.match(styles, /\.settings-field--counted\s*\{[^}]*padding-bottom:\s*22px/s)
})

test('辖区档案连续区块由父级 gap 分隔且筛选不游离在工具栏外', () => {
  const registrySource = readFileSync(
    new URL('../src/pages/RegistryManagement.tsx', import.meta.url),
    'utf8',
  )
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')

  assert.match(registrySource, /className="registry-management__content"/)
  assert.match(registrySource, /filters=\{toolbarFilters\}/)
  assert.doesNotMatch(registrySource, /<Space wrap className="mb-3">/)
  assert.match(styles, /\.registry-management__content\s*\{[^}]*display:\s*grid;[^}]*gap:\s*16px/s)
  assert.match(styles, /@media \(max-width: 767px\)[\s\S]*?\.registry-management__content\s*\{[^}]*gap:\s*12px/s)
})

test('在线查询桌面端把数据范围控件放在工作表状态提示左侧', () => {
  const querySource = readFileSync(
    new URL('../src/pages/DataQuery.tsx', import.meta.url),
    'utf8',
  )
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')

  const toolbarStart = querySource.indexOf('<div className="query-spreadsheet-toolbar">')
  const scopeStart = querySource.indexOf('query-spreadsheet-toolbar__scope', toolbarStart)
  const statusHint = querySource.indexOf('选择单元格或整行后，可在这里查看来源状态和行操作', toolbarStart)

  assert.ok(toolbarStart >= 0)
  assert.ok(scopeStart > toolbarStart)
  assert.ok(statusHint > scopeStart)
  assert.match(styles, /\.query-spreadsheet-toolbar\s*\{[^}]*flex-wrap:\s*wrap/s)
  assert.match(styles, /\.query-spreadsheet-toolbar__type\s*\{[^}]*width:\s*176px/s)
})

test('在线查询使用 Univer 原生入口触发后台全量排序并在切换数据范围时清除', () => {
  const querySource = readFileSync(
    new URL('../src/pages/DataQuery.tsx', import.meta.url),
    'utf8',
  )
  const sheetSource = readFileSync(
    new URL('../src/components/QuerySpreadsheet.tsx', import.meta.url),
    'utf8',
  )

  assert.doesNotMatch(querySource, /placeholder="选择排序字段"/)
  assert.match(querySource, /sort_by:\s*sortBy/)
  assert.match(querySource, /sort_order:\s*sortBy \? sortOrder : undefined/)
  assert.match(querySource, /按 \{sortBy\} \{sortOrder === 'asc' \? '升序' : '降序'\}/)
  assert.match(querySource, /清除排序/)
  assert.match(sheetSource, /UniverSheetsSortPreset/)
  assert.match(sheetSource, /Event\.SheetBeforeRangeSort/)
  assert.match(sheetSource, /params\.cancel = true/)
  assert.match(sheetSource, /resolveQuerySheetSortRequest/)
  assert.match(sheetSource, /onSortChange\(sortRequest\.column, sortRequest\.order\)/)

  const sourceReset = querySource.slice(
    querySource.indexOf('const changeSourceType'),
    querySource.indexOf('const changeBusinessType'),
  )
  const typeReset = querySource.slice(
    querySource.indexOf('const changeBusinessType'),
    querySource.indexOf('return (', querySource.indexOf('const changeBusinessType')),
  )
  assert.match(sourceReset, /setSortBy\(undefined\)/)
  assert.match(typeReset, /setSortBy\(undefined\)/)
})

test('下发导入工作台提供原始与已处理数据模式', () => {
  const panelSource = readFileSync(
    new URL('../src/components/PoliceDispatchPanel.tsx', import.meta.url),
    'utf8',
  )
  const clientSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(panelSource, /已处理数据直接下发/)
  assert.match(panelSource, /确认导入并进入待发布/)
  assert.match(panelSource, /\/police-tasks\?batch=/)
  assert.match(clientSource, /form\.append\('import_mode', importMode\)/)
})
