import { isValidElement, type ReactNode } from 'react'
import type { ColumnGroupType, ColumnType, ColumnsType } from 'antd/es/table'
import type { ResponsiveLayoutMode } from '../hooks/useResponsiveLayout'

export type ResponsiveColumnPriority = 'always' | 'standard' | 'wide'

export type ResponsiveColumn<T extends object> = (ColumnType<T> | ColumnGroupType<T>) & {
  responsivePriority?: ResponsiveColumnPriority
  responsiveAction?: boolean
}

export type ResponsiveColumns<T extends object> = Array<ResponsiveColumn<T>>

function isGroup<T extends object>(column: ResponsiveColumn<T>): column is ColumnGroupType<T> & {
  responsivePriority?: ResponsiveColumnPriority
  responsiveAction?: boolean
} {
  return Array.isArray((column as ColumnGroupType<T>).children)
}

function flattenColumns<T extends object>(columns: ColumnsType<T>): ResponsiveColumn<T>[] {
  return columns.flatMap(item => {
    const column = item as ResponsiveColumn<T>
    return isGroup(column)
      ? flattenColumns(column.children as ColumnsType<T>)
      : [column]
  })
}

function shouldShow(priority: ResponsiveColumnPriority | undefined, mode: ResponsiveLayoutMode) {
  if (!priority || priority === 'always') return true
  if (priority === 'standard') return mode !== 'compact'
  return mode === 'wide'
}

function isActionColumn<T extends object>(column: ResponsiveColumn<T>) {
  const key = String(column.key ?? '')
  const title = typeof column.title === 'string' ? column.title : ''
  return column.responsiveAction === true || key === 'action' || key === 'actions' || title === '操作'
}

function normalizeColumn<T extends object>(column: ResponsiveColumn<T>): ResponsiveColumn<T> {
  if (!isActionColumn(column)) return column
  return {
    ...column,
    responsivePriority: 'always',
    fixed: column.fixed ?? 'right',
    width: column.width ?? 112,
  }
}

export function getResponsiveColumns<T extends object>(
  columns: ColumnsType<T>,
  mode: ResponsiveLayoutMode,
): ColumnsType<T> {
  const visit = (items: ColumnsType<T>): ColumnsType<T> => items.flatMap(item => {
    const column = normalizeColumn(item as ResponsiveColumn<T>)
    if (!shouldShow(column.responsivePriority, mode)) return []
    if (!isGroup(column)) return [column as ColumnType<T>]
    const children = visit(column.children as ColumnsType<T>)
    if (!children.length) return []
    return [{ ...column, children } as ColumnGroupType<T>]
  })
  return visit(columns)
}

function readDataIndex(record: object, dataIndex: ColumnType<any>['dataIndex']) {
  if (dataIndex == null) return undefined
  const path = Array.isArray(dataIndex) ? dataIndex : [dataIndex]
  return path.reduce<unknown>((value, key) => (
    value == null ? undefined : (value as Record<string | number, unknown>)[key]
  ), record)
}

export function renderResponsiveColumnValue<T extends object>(
  column: ResponsiveColumn<T>,
  record: T,
): ReactNode {
  const value = readDataIndex(record, (column as ColumnType<T>).dataIndex)
  if (value == null || value === '') return '—'
  if (column.render) {
    const rendered = column.render(value, record, 0) as unknown
    if (rendered == null || typeof rendered === 'string' || typeof rendered === 'number' || typeof rendered === 'boolean' || isValidElement(rendered)) {
      return rendered as ReactNode
    }
    if (typeof rendered === 'object' && 'children' in rendered) {
      return (rendered as { children?: ReactNode }).children || '—'
    }
    return String(rendered)
  }
  return String(value)
}

export function getHiddenResponsiveColumns<T extends object>(
  columns: ColumnsType<T>,
  mode: ResponsiveLayoutMode,
): ResponsiveColumn<T>[] {
  return flattenColumns(columns).filter(column => (
    column.responsivePriority === 'standard' && mode === 'compact'
      || column.responsivePriority === 'wide' && mode !== 'wide'
  ))
}
