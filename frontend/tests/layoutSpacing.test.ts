import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

function tsxFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const target = join(directory, entry.name)
    if (entry.isDirectory()) return tsxFiles(target)
    return entry.name.endsWith('.tsx') ? [target] : []
  })
}

function jsxTagName(node: ts.JsxTagNameExpression) {
  return node.getText()
}

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
  assert.match(styles, /\.list-content\s*\{[^}]*display:\s*grid;[^}]*gap:\s*16px/s)
  assert.match(styles, /@media \(max-width: 767px\)[\s\S]*?\.list-content\s*\{[^}]*gap:\s*12px/s)
  assert.match(styles, /@media \(max-width: 767px\)[\s\S]*?\.list-toolbar__filters[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\)/)

  assert.match(settingsSource, /settings-field--counted/)
  assert.match(settingsSource, /settings-field__hint/)
  assert.match(styles, /\.settings-field--counted\s*\{[^}]*padding-bottom:\s*22px/s)
})

test('运维备份提示和设置面板使用稳定的父级区块间距', () => {
  const operationsSource = readFileSync(
    new URL('../src/pages/OperationsCenter.tsx', import.meta.url),
    'utf8',
  )
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')

  assert.match(operationsSource, /<div className="operations-backups-content">[\s\S]*<Alert[\s\S]*每日自动备份/)
  assert.match(styles, /\.operations-backups-content\s*\{[^}]*display:\s*grid;[^}]*gap:\s*20px/s)
  assert.match(styles, /@media \(max-width: 767px\)[\s\S]*?\.operations-backups-content\s*\{[^}]*gap:\s*12px/s)
})

test('Panel 中的列表工具栏必须由统一列表内容容器承接结果区间距', () => {
  const pagesDirectory = fileURLToPath(new URL('../src/pages/', import.meta.url))
  const violations: string[] = []

  for (const file of tsxFiles(pagesDirectory)) {
    const sourceText = readFileSync(file, 'utf8')
    const source = ts.createSourceFile(file, sourceText, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)

    const visit = (node: ts.Node) => {
      if (ts.isJsxElement(node) && jsxTagName(node.openingElement.tagName) === 'Panel') {
        for (const child of node.children) {
          if (ts.isJsxSelfClosingElement(child) && jsxTagName(child.tagName) === 'ListToolbar') {
            const { line } = source.getLineAndCharacterOfPosition(child.getStart(source))
            violations.push(`${file}:${line + 1}`)
          }
        }
      }
      ts.forEachChild(node, visit)
    }
    visit(source)
  }

  assert.deepEqual(violations, [], `ListToolbar 不能直接作为 Panel 子项：\n${violations.join('\n')}`)
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

test('详情抽屉和高密度工作台不使用容易贴边的 space-y 分隔', () => {
  const registrySource = readFileSync(
    new URL('../src/pages/RegistryManagement.tsx', import.meta.url),
    'utf8',
  )
  const querySource = readFileSync(
    new URL('../src/pages/DataQuery.tsx', import.meta.url),
    'utf8',
  )
  const dispatchSource = readFileSync(
    new URL('../src/components/PoliceDispatchPanel.tsx', import.meta.url),
    'utf8',
  )
  const operationsSource = readFileSync(
    new URL('../src/pages/OperationsCenter.tsx', import.meta.url),
    'utf8',
  )
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')

  assert.match(registrySource, /<div className="registry-detail">/)
  assert.doesNotMatch(registrySource, /<div className="space-y-5">[\s\S]*?<Descriptions bordered/)
  assert.match(querySource, /<div className="query-drawer-content">/)
  assert.match(dispatchSource, /<div className="police-dispatch-panel__content">/)
  assert.match(dispatchSource, /<div className="police-dispatch-panel__preview">/)
  assert.match(operationsSource, /<div className="operations-log-layout">/)
  assert.match(operationsSource, /<div className="operations-center-layout">/)
  assert.match(registrySource, /<div className="registry-management-layout">/)
  assert.match(styles, /\.registry-detail,[\s\S]*?\.operations-center-layout\s*\{[^}]*display:\s*grid;[^}]*gap:\s*20px/s)
  assert.match(styles, /\.registry-management-layout,[\s\S]*?\.operations-log-layout\s*\{[^}]*display:\s*grid;[^}]*gap:\s*16px/s)
  assert.match(styles, /\.police-dispatch-panel__content\s*\{[^}]*display:\s*grid;[^}]*gap:\s*20px[^}]*padding:\s*20px/s)
  assert.match(styles, /@media \(max-width: 767px\)[\s\S]*?\.registry-detail,[\s\S]*?\.operations-center-layout\s*\{[^}]*gap:\s*12px/s)
})

test('移动端卡片列表使用父级 gap，避免卡片或表格贴在一起', () => {
  const sources = [
    readFileSync(new URL('../src/pages/Dashboard.tsx', import.meta.url), 'utf8'),
    readFileSync(new URL('../src/pages/DataQuery.tsx', import.meta.url), 'utf8'),
    readFileSync(new URL('../src/pages/WorkLogDrafts.tsx', import.meta.url), 'utf8'),
    readFileSync(new URL('../src/components/MobileReportTable.tsx', import.meta.url), 'utf8'),
  ]
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')

  assert.match(sources[0], /dashboard-mobile-detail-list/)
  assert.match(sources[0], /dashboard-mobile-report-list/)
  assert.match(sources[1], /data-query-mobile-list/)
  assert.match(sources[2], /work-log-drafts__mobile-list/)
  assert.match(sources[3], /mobile-report-table__layout/)
  assert.match(styles, /\.dashboard-mobile-detail-list,[\s\S]*?\.mobile-report-table__layout\s*\{[^}]*display:\s*grid;[^}]*gap:\s*12px/s)
  assert.match(styles, /@media \(max-width: 767px\)[\s\S]*?\.dashboard-mobile-detail-list,[\s\S]*?\.mobile-report-table__layout\s*\{[^}]*gap:\s*10px/s)
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
  assert.match(panelSource, /确认导入并发布可发布项/)
  assert.match(panelSource, /\/police-tasks\?batch=/)
  assert.match(clientSource, /form\.append\('import_mode', importMode\)/)
})

test('外部数据获取入口使用统一面板结构', () => {
  const componentSource = readFileSync(
    new URL('../src/components/ExternalDataPanel.tsx', import.meta.url),
    'utf8',
  )
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')
  const consumers = [
    '../src/pages/CodeSummary.tsx',
    '../src/components/VisitSourcePanel.tsx',
    '../src/components/SyncPanel.tsx',
    '../src/pages/RegistryManagement.tsx',
    '../src/pages/WorkflowConfig.tsx',
  ].map(path => readFileSync(new URL(path, import.meta.url), 'utf8'))

  assert.match(componentSource, /function ExternalDataPanel/)
  assert.match(componentSource, /ExternalDataStatus/)
  assert.match(componentSource, /ExternalDataProgress/)
  for (const source of consumers) assert.match(source, /<ExternalDataPanel/)

  assert.match(styles, /\.external-data-panel__controls\s*\{/)
  assert.match(styles, /\.external-data-panel__stats\s*\{/)
  assert.match(styles, /\.external-data-panel__progress[\s\S]*color-mix/)
  assert.match(styles, /\.external-data-panel--embedded\s*\{/)
  assert.match(styles, /@media \(max-width: 767px\)[\s\S]*?\.external-data-panel__stats[\s\S]*?repeat\(2, minmax\(0, 1fr\)\)/)
})
