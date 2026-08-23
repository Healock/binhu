import assert from 'node:assert/strict'
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
