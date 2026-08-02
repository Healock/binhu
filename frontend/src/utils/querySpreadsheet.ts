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

export const QUERY_SHEET_FEATURE_CONFIG = {
  disableForceStringAlert: true,
  disableForceStringMark: true,
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

export function querySheetPalette(darkMode: boolean): QuerySheetPalette {
  return darkMode
    ? {
        background: '#0f172a',
        border: '#334155',
        conflict: '#3f1d25',
        editable: '#142a44',
        header: '#1e293b',
        pending: '#3a2f13',
        text: '#e5edf7',
      }
    : {
        background: '#ffffff',
        border: '#d8dee9',
        conflict: '#fff1f0',
        editable: '#f0f7ff',
        header: '#e8eef8',
        pending: '#fffbe6',
        text: '#172033',
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
