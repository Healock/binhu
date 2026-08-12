import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('工单和系统设置页面使用明确的间距布局', () => {
  const workflowSource = readFileSync(
    new URL('../src/pages/WorkflowTickets.tsx', import.meta.url),
    'utf8',
  )
  const settingsSource = readFileSync(
    new URL('../src/pages/SystemSettings.tsx', import.meta.url),
    'utf8',
  )
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')

  assert.match(workflowSource, /workflow-photo-toolbar__filters/)
  assert.match(workflowSource, /workflow-photo-toolbar__actions/)
  assert.match(workflowSource, /workflow-ticket-detail__section/)
  assert.match(styles, /\.workflow-ticket-detail\s*\{[^}]*gap:\s*24px/s)
  assert.match(styles, /\.workflow-photo-toolbar__filters,[\s\S]*?gap:\s*12px/)

  assert.match(settingsSource, /settings-field--counted/)
  assert.match(settingsSource, /settings-field__hint/)
  assert.match(styles, /\.settings-field--counted\s*\{[^}]*padding-bottom:\s*22px/s)
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
  assert.match(panelSource, /导入并生成待发布任务/)
  assert.match(clientSource, /form\.append\('import_mode', importMode\)/)
})
