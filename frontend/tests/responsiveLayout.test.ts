import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { getResponsiveColumns } from '../src/components/responsiveTable.ts'
import { getResponsiveLayoutMode } from '../src/hooks/useResponsiveLayout.ts'

test('响应式布局按实际容器宽度和视口高度分档', () => {
  assert.equal(getResponsiveLayoutMode(1199, 900), 'compact')
  assert.equal(getResponsiveLayoutMode(1400, 760), 'compact')
  assert.equal(getResponsiveLayoutMode(1400, 761), 'standard')
  assert.equal(getResponsiveLayoutMode(1600, 900), 'wide')
})

test('紧凑表格保留操作列并把低优先级列留给详情', () => {
  const columns = getResponsiveColumns([
    { title: '姓名', dataIndex: 'name', responsivePriority: 'always' },
    { title: '社区', dataIndex: 'community', responsivePriority: 'standard' },
    { title: '备注', dataIndex: 'note', responsivePriority: 'wide' },
    { title: '操作', key: 'actions', render: () => '详情' },
  ], 'compact') as Array<Record<string, unknown>>

  assert.deepEqual(columns.map(column => column.title), ['姓名', '操作'])
  assert.equal(columns[1].fixed, 'right')
  assert.equal(columns[1].width, 112)
})

test('标准和宽屏逐步恢复响应式列', () => {
  const columns = [
    { title: '姓名', dataIndex: 'name', responsivePriority: 'always' as const },
    { title: '社区', dataIndex: 'community', responsivePriority: 'standard' as const },
    { title: '备注', dataIndex: 'note', responsivePriority: 'wide' as const },
  ]

  assert.deepEqual(
    (getResponsiveColumns(columns, 'standard') as Array<{ title?: string }>).map(column => column.title),
    ['姓名', '社区'],
  )
  assert.deepEqual(
    (getResponsiveColumns(columns, 'wide') as Array<{ title?: string }>).map(column => column.title),
    ['姓名', '社区', '备注'],
  )
})

test('桌面收缩侧栏和表格展开按钮保持稳定尺寸', () => {
  const layoutSource = readFileSync(new URL('../src/components/Layout.tsx', import.meta.url), 'utf8')
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')

  assert.match(layoutSource, /className="app-sidebar__header /)
  assert.match(layoutSource, /className="app-sidebar__footer /)
  assert.match(layoutSource, /className="app-sidebar__footer-main /)
  assert.match(styles, /\.app-sidebar--collapsed \.app-sidebar__brand\s*\{[^}]*display:\s*none;/s)
  assert.match(styles, /\.app-sidebar--collapsed \.app-sidebar__footer-main\s*\{[^}]*flex-direction:\s*column;[^}]*gap:\s*6px;/s)
  assert.match(styles, /button:not\(\.ant-btn\)[^{]*:not\(\.ant-table-row-expand-icon\)/)
  assert.match(styles, /\.app-shell \.ant-table-row-expand-icon-cell\s*\{[^}]*min-width:\s*40px !important;/s)
  assert.match(styles, /\.app-shell \.ant-table-row-expand-icon\s*\{[^}]*width:\s*17px;[^}]*height:\s*17px;[^}]*min-height:\s*17px;/s)
  assert.match(styles, /\.app-shell \.ant-table-row-expand-icon::before,[\s\r\n]+\.app-shell \.ant-table-row-expand-icon::after\s*\{[^}]*top:\s*50%;[^}]*left:\s*50%;[^}]*width:\s*7px;[^}]*height:\s*1px;/s)
  assert.match(styles, /\.app-shell \.ant-table-row-expand-icon-collapsed::after\s*\{[^}]*rotate\(90deg\)/s)
})
