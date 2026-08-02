import type { QueryDataRow } from '../api/client.ts'
import {
  canEditQueryCell,
  createQueryDraftRow,
  isQueryDraftTouched,
  type QueryDisplayRow,
} from './queryGrid.ts'

export type QuerySheetRowKind = 'data' | 'draft' | 'blank'

export interface QuerySheetRow {
  kind: QuerySheetRowKind
  data: QueryDisplayRow
}

export interface QuerySheetCellChange {
  row: QueryDisplayRow
  column: string
  before: string
  after: string
}

export interface QuerySheetCustomFilter {
  val: string | number
  operator?: string
}

export interface QuerySheetFilterCriteria {
  colId: number
  filters?: {
    blank?: true
    filters?: string[]
  }
  colorFilters?: {
    cellFillColors?: Array<string | null>
    cellTextColors?: string[]
  }
  customFilters?: {
    and?: number
    customFilters: QuerySheetCustomFilter[]
  }
}

export interface QuerySheetRequestFilters {
  filters?: Record<string, string[]>
  gridFilters?: Record<string, unknown>
  unsupportedColorColumns: string[]
}

export const QUERY_SHEET_FEATURE_CONFIG = {
  disableForceStringAlert: true,
  disableForceStringMark: true,
}

export const QUERY_SHEET_UI_CONFIG = {
  // Univer 0.25.x 只有在 header 和 toolbar 同时启用时才渲染顶部功能区。
  header: true,
  toolbar: true,
  ribbonType: 'classic' as const,
  footer: false,
  formulaBar: true,
  contextMenu: false,
}

export async function toggleQuerySheetFullscreen(
  target: HTMLElement | null,
  activeElement: Element | null,
  exitFullscreen?: () => Promise<void>,
): Promise<void> {
  if (!target) throw new Error('fullscreen_target_missing')
  if (activeElement === target) {
    if (!exitFullscreen) throw new Error('fullscreen_not_supported')
    await exitFullscreen()
    return
  }
  if (activeElement) {
    if (!exitFullscreen) throw new Error('fullscreen_not_supported')
    await exitFullscreen()
  }
  if (typeof target.requestFullscreen !== 'function') {
    throw new Error('fullscreen_not_supported')
  }
  await target.requestFullscreen()
}

export interface QuerySheetPalette {
  background: string
  border: string
  conflict: string
  editable: string
  header: string
  pending: string
  text: string
}

export function querySheetPalette(_darkMode: boolean): QuerySheetPalette {
  // Univer 会在深色模式下转换工作表源颜色。这里始终写入同一套语义色，
  // 避免先写深色、再被 Univer 二次转换成浅色。
  return {
    background: '#ffffff',
    border: '#d8dee9',
    conflict: '#fff1f0',
    editable: '#f0f7ff',
    header: '#e8eef8',
    pending: '#fffbe6',
    text: '#172033',
  }
}

function hasColorFilter(criteria: QuerySheetFilterCriteria): boolean {
  const colors = criteria.colorFilters
  return Boolean(colors?.cellFillColors?.length || colors?.cellTextColors?.length)
}

function customFilterToGridCondition(filter: QuerySheetCustomFilter): Record<string, string> {
  const operator = String(filter.operator || 'equal')
  const rawValue = String(filter.val ?? '')
  if (rawValue === '') {
    return { type: operator === 'notEqual' ? 'notBlank' : 'blank', filter: '' }
  }

  const startsWithWildcard = rawValue.startsWith('*')
  const endsWithWildcard = rawValue.endsWith('*')
  const value = rawValue.replace(/^\*/, '').replace(/\*$/, '')
  if (operator === 'equal' || operator === 'notEqual') {
    if (startsWithWildcard && endsWithWildcard) {
      return {
        type: operator === 'notEqual' ? 'notContains' : 'contains',
        filter: value,
      }
    }
    if (endsWithWildcard) {
      return {
        type: operator === 'notEqual' ? 'notStartsWith' : 'startsWith',
        filter: value,
      }
    }
    if (startsWithWildcard) {
      return {
        type: operator === 'notEqual' ? 'notEndsWith' : 'endsWith',
        filter: value,
      }
    }
    return { type: operator === 'notEqual' ? 'notEqual' : 'equals', filter: rawValue }
  }

  const comparisonTypes: Record<string, string> = {
    greaterThan: 'greaterThan',
    greaterThanOrEqual: 'greaterThanOrEqual',
    lessThan: 'lessThan',
    lessThanOrEqual: 'lessThanOrEqual',
  }
  return {
    type: comparisonTypes[operator] || 'equals',
    filter: rawValue,
  }
}

export function buildQuerySheetRequestFilters(
  criteriaByColumn: Record<string, QuerySheetFilterCriteria>,
): QuerySheetRequestFilters {
  const filters: Record<string, string[]> = {}
  const gridFilters: Record<string, unknown> = {}
  const unsupportedColorColumns: string[] = []

  for (const [column, criteria] of Object.entries(criteriaByColumn)) {
    if (hasColorFilter(criteria)) unsupportedColorColumns.push(column)

    const selectedValues = [...(criteria.filters?.filters || [])]
    if (criteria.filters?.blank) selectedValues.push('')
    if (selectedValues.length) filters[column] = [...new Set(selectedValues.map(String))]

    const customFilters = criteria.customFilters?.customFilters || []
    if (customFilters.length === 1) {
      gridFilters[column] = customFilterToGridCondition(customFilters[0])
    } else if (customFilters.length > 1) {
      gridFilters[column] = {
        operator: criteria.customFilters?.and ? 'and' : 'or',
        conditions: customFilters.slice(0, 2).map(customFilterToGridCondition),
      }
    }
  }

  return {
    filters: Object.keys(filters).length ? filters : undefined,
    gridFilters: Object.keys(gridFilters).length ? gridFilters : undefined,
    unsupportedColorColumns,
  }
}

interface UniverBorderEnums {
  BorderStyleTypes?: {
    THIN?: unknown
  }
}

export function resolveQuerySheetThinBorderStyle(enums: UniverBorderEnums): number | null {
  const style = enums.BorderStyleTypes?.THIN
  return typeof style === 'number' ? style : null
}

function stringifyCell(value: unknown): string {
  return value === null || value === undefined ? '' : String(value)
}

export function buildQuerySheetRows(
  rows: QueryDataRow[],
  drafts: QueryDisplayRow[],
  columns: string[],
  canAdd: boolean,
  createDraftId: (rowOffset: number) => string,
  minimumBlankRows = 20,
): QuerySheetRow[] {
  const result: QuerySheetRow[] = rows.map(row => ({
    kind: 'data',
    data: { ...row, __kind: 'parent' },
  }))

  for (const draft of drafts.filter(row => isQueryDraftTouched(row, columns))) {
    result.push({ kind: 'draft', data: { ...draft, __kind: 'draft' } })
  }

  if (canAdd) {
    for (let index = 0; index < minimumBlankRows; index += 1) {
      result.push({
        kind: 'blank',
        data: createQueryDraftRow(columns, createDraftId(index)),
      })
    }
  }

  return result
}

export function canEditQuerySheetCell(
  source: 'online' | 'archive',
  row: QuerySheetRow | undefined,
  column: string | undefined,
  canAdd: boolean,
): boolean {
  if (!row || !column) return false
  return canEditQueryCell(source, row.data, column, canAdd)
}

export function parseQuerySheetClipboard(text: string): string[][] {
  const normalized = text.replace(/\r\n?/g, '\n')
  const lines = normalized.endsWith('\n')
    ? normalized.slice(0, -1).split('\n')
    : normalized.split('\n')
  return lines.map(line => line.split('\t'))
}

export function isQuerySheetRangeEditable(
  source: 'online' | 'archive',
  sheetRows: QuerySheetRow[],
  columns: string[],
  startRow: number,
  startColumn: number,
  rowCount: number,
  columnCount: number,
  canAdd: boolean,
): boolean {
  if (startRow < 1 || startColumn < 0) return false
  for (let rowOffset = 0; rowOffset < rowCount; rowOffset += 1) {
    for (let columnOffset = 0; columnOffset < columnCount; columnOffset += 1) {
      const row = sheetRows[startRow - 1 + rowOffset]
      const column = columns[startColumn + columnOffset]
      if (!canEditQuerySheetCell(source, row, column, canAdd)) return false
    }
  }
  return true
}

export function applyQuerySheetValues(
  sheetRows: QuerySheetRow[],
  columns: string[],
  values: unknown[][],
): QuerySheetCellChange[] {
  const changes: QuerySheetCellChange[] = []
  for (let rowOffset = 0; rowOffset < sheetRows.length; rowOffset += 1) {
    const descriptor = sheetRows[rowOffset]
    for (let columnIndex = 0; columnIndex < columns.length; columnIndex += 1) {
      const column = columns[columnIndex]
      const before = stringifyCell(descriptor.data[column])
      const after = stringifyCell(values[rowOffset]?.[columnIndex])
      if (before === after) continue
      if (descriptor.kind === 'blank') descriptor.kind = 'draft'
      descriptor.data[column] = after
      changes.push({ row: descriptor.data, column, before, after })
    }
  }
  return changes
}

export function updateQuerySheetDrafts(
  sheetRows: QuerySheetRow[],
  columns: string[],
): QueryDisplayRow[] {
  return sheetRows
    .filter(row => row.kind === 'draft' && isQueryDraftTouched(row.data, columns))
    .map(row => ({ ...row.data }))
}

export function selectedQuerySheetRow(
  sheetRows: QuerySheetRow[],
  worksheetRow: number,
): QueryDisplayRow | null {
  if (worksheetRow < 1) return null
  return sheetRows[worksheetRow - 1]?.data || null
}
