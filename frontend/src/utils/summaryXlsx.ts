import type {
  Cell,
  CellObject,
  Sheet,
  SheetData,
} from 'write-excel-file/browser'
import { downloadBlob } from './fileDownload.ts'

export type SummaryExportRow = Record<string, unknown>

export interface SummaryExportTable {
  sheet: string
  columns: string[]
  rows: readonly SummaryExportRow[]
  total?: SummaryExportRow | null
  /** Highlight the three lowest numeric values in each requested column. */
  highlightLowestColumns?: readonly string[]
}

export interface SummaryWorkbookOptions {
  fileName: string
  tables: SummaryExportTable[]
}

const RATE_COLUMN_PATTERN = /率$/
const TWO_DECIMAL_COLUMNS = new Set(['每日人均核查数', '当日人均核查数'])
const ONE_DECIMAL_COLUMN_PATTERN = /^(在岗人日|人均|户均)/
const INVALID_FILE_NAME = /[\\/:*?"<>|]/g

const borderStyle = {
  borderColor: '#cbd5e1',
  borderStyle: 'thin' as const,
}

function visibleColumns(columns: readonly string[]): string[] {
  return columns.filter(column => column !== 'id' && !column.startsWith('_'))
}

function textWidth(value: unknown): number {
  return Array.from(String(value ?? '')).reduce(
    (width, char) => width + (/[^\u0000-\u00ff]/.test(char) ? 2 : 1),
    0,
  )
}

function columnWidth(column: string, rows: readonly SummaryExportRow[]): number {
  const contentWidth = rows.reduce(
    (width, row) => Math.max(width, textWidth(row[column])),
    textWidth(column),
  )
  return Math.min(Math.max(contentWidth + 2, 10), 24)
}

function numberFormat(column: string, value: number): string {
  if (RATE_COLUMN_PATTERN.test(column)) return '0.0%'
  if (TWO_DECIMAL_COLUMNS.has(column)) return '0.00'
  if (ONE_DECIMAL_COLUMN_PATTERN.test(column)) return '0.0'
  return Number.isInteger(value) ? '#,##0' : '0.00'
}

function dataCell(
  value: unknown,
  column: string,
  total: boolean,
  highlighted = false,
): Cell {
  const style = total
    ? {
        ...borderStyle,
        backgroundColor: '#eff6ff',
        fontWeight: 'bold' as const,
      }
    : {
        ...borderStyle,
        ...(highlighted ? { backgroundColor: '#fff2cc' } : {}),
      }

  if (value == null || value === '') {
    return { value: '', ...style }
  }
  if (typeof value === 'number' && Number.isFinite(value)) {
    return {
      value,
      type: Number,
      format: numberFormat(column, value),
      align: 'right',
      ...style,
    }
  }
  if (typeof value === 'boolean') {
    return { value, type: Boolean, align: 'center', ...style }
  }
  return {
    value: String(value),
    type: String,
    wrap: true,
    ...style,
  }
}

function headerCell(column: string): CellObject {
  return {
    value: column,
    type: String,
    fontWeight: 'bold',
    textColor: '#ffffff',
    backgroundColor: '#2563eb',
    align: 'center',
    alignVertical: 'center',
    wrap: true,
    ...borderStyle,
  }
}

export function buildSummarySheet(table: SummaryExportTable): Sheet<Blob> {
  const columns = visibleColumns(table.columns)
  const rows = table.total ? [...table.rows, table.total] : [...table.rows]
  const highlightColumns = new Set(
    (table.highlightLowestColumns || []).filter(column => columns.includes(column)),
  )
  const highlightedCells = new Set<string>()
  for (const column of highlightColumns) {
    table.rows
      .map((row, rowIndex) => ({
        rowIndex,
        value: row[column],
      }))
      .filter(({ value }) => typeof value === 'number' && Number.isFinite(value))
      .sort((left, right) => left.value - right.value || left.rowIndex - right.rowIndex)
      .slice(0, 3)
      .forEach(({ rowIndex }) => highlightedCells.add(`${rowIndex}:${column}`))
  }
  const data: SheetData = [
    columns.map(headerCell),
    ...table.rows.map((row, rowIndex) => columns.map(column => dataCell(
      row[column],
      column,
      false,
      highlightedCells.has(`${rowIndex}:${column}`),
    ))),
  ]
  if (table.total) {
    data.push(columns.map(column => dataCell(table.total?.[column], column, true)))
  }

  return {
    data,
    sheet: table.sheet,
    columns: columns.map(column => ({ width: columnWidth(column, rows) })),
    stickyRowsCount: 1,
    showGridLines: false,
    orientation: columns.length > 8 ? 'landscape' : undefined,
  }
}

export function normalizeXlsxFileName(fileName: string): string {
  const safeName = fileName.trim().replace(INVALID_FILE_NAME, '_') || '汇总数据'
  return safeName.toLowerCase().endsWith('.xlsx') ? safeName : `${safeName}.xlsx`
}

export function buildSummaryWorkbook(options: SummaryWorkbookOptions) {
  return {
    fileName: normalizeXlsxFileName(options.fileName),
    sheets: options.tables.map(buildSummarySheet),
  }
}

export async function exportSummaryWorkbook(options: SummaryWorkbookOptions): Promise<void> {
  const { default: writeXlsxFile } = await import('write-excel-file/browser')
  const workbook = buildSummaryWorkbook(options)
  const blob = await writeXlsxFile(workbook.sheets, {
    fontFamily: 'Microsoft YaHei',
    fontSize: 10,
  }).toBlob()
  await downloadBlob(blob, workbook.fileName)
}
