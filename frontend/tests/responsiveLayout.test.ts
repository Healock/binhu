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

test('桌面端顶部消息和通知避开自定义标题栏', () => {
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')

  assert.match(styles, /html\.desktop-shell \.ant-message\s*\{[^}]*top:\s*56px\s*!important;/s)
  assert.match(styles, /html\.desktop-shell \.ant-notification-top,[\s\S]*?\.ant-notification-topRight\s*\{[^}]*top:\s*56px\s*!important;/s)
})

test('短桌面窗口的登录页可滚动且不会裁掉底部操作', () => {
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')

  assert.match(styles, /@media \(max-height: 720px\) and \(min-width: 901px\)[\s\S]*?\.desktop-app-content \.login-page\s*\{[^}]*overflow-y:\s*auto;/s)
  assert.match(styles, /@media \(max-height: 720px\) and \(min-width: 901px\)[\s\S]*?\.desktop-app-content \.login-form-panel\s*\{[^}]*align-items:\s*flex-start;/s)
  assert.match(styles, /@media \(max-height: 680px\) and \(min-width: 901px\)[\s\S]*?\.login-product-brand img\s*\{[^}]*width:\s*60px;[^}]*height:\s*56px;/s)
})

test('任务分配和行内编辑在紧凑桌面宽度保持完整可操作', () => {
  const assignmentSource = readFileSync(new URL('../src/components/MobileTaskAssignmentWorkbench.tsx', import.meta.url), 'utf8')
  const taskTableSource = readFileSync(new URL('../src/components/MobileTaskTable.tsx', import.meta.url), 'utf8')
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')

  assert.match(assignmentSource, /icon=\{<CloseOutlined \/>\}[\s\S]*?>\s*退出分配\s*<\/Button>/)
  assert.match(taskTableSource, /getResponsiveColumns\(columns, responsiveLayout\.mode\)/)
  assert.match(taskTableSource, /compactPersonnelColumns[\s\S]*?responsiveLayout\.isCompact \? \[\{[\s\S]*?title: '人员信息'/)
  assert.match(taskTableSource, /key: 'compact_personnel'[\s\S]*?fixed: 'left'/)
  assert.match(taskTableSource, /const columns:[\s\S]*?\.\.\.compactPersonnelColumns,[\s\S]*?title: '截止日期'/)
  assert.match(taskTableSource, /mobile-task-table-personnel__[\s\S]*?task\.summary\.identity_number/)
  assert.match(taskTableSource, /mobileTaskPhoneOptions\(task\.summary\.phone\)/)
  assert.match(taskTableSource, /mobile-task-table-personnel__row--address[\s\S]*?task\.summary\.original_address/)
  assert.match(taskTableSource, /!responsiveLayout\.isCompact \? \[[\s\S]*?title: '姓名'[\s\S]*?title: '身份证号码'[\s\S]*?title: '电话'[\s\S]*?title: '地址'/)
  assert.doesNotMatch(taskTableSource, /key: 'identity_number'[\s\S]{0,120}responsivePriority: 'wide'/)
  assert.doesNotMatch(taskTableSource, /key: 'phone'[\s\S]{0,120}responsivePriority: 'standard'/)
  assert.match(styles, /\.mobile-task-table-personnel\s*\{[^}]*display:\s*grid;[^}]*min-width:\s*0;[^}]*gap:\s*5px;/s)
  assert.match(styles, /\.mobile-task-table-personnel__row\s*\{[^}]*grid-template-columns:\s*64px minmax\(0, 1fr\);/s)
  assert.match(styles, /\.mobile-task-table-personnel__row dd\s*\{[^}]*overflow-wrap:\s*anywhere;[^}]*white-space:\s*normal;/s)
  assert.match(styles, /\.mobile-task-table-personnel__phones\s*\{[^}]*flex-wrap:\s*wrap;/s)
  assert.match(styles, /\.mobile-task-assignment-workbench__toolbar\s*\{[^}]*flex-wrap:\s*wrap;/s)
  assert.match(styles, /\.mobile-task-table-inline-editor\s*\{[^}]*width:\s*100%;[^}]*min-width:\s*0;/s)
  assert.doesNotMatch(styles, /\.mobile-task-table-inline-editor\s*\{[^}]*min-width:\s*1044px;/s)
})
