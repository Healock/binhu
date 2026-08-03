const SUMMARY_IDENTITY_COLUMNS = new Set(['社区', '姓名'])

export function visitSummaryColumnWidth(column: string): number {
  if (SUMMARY_IDENTITY_COLUMNS.has(column)) return 120
  if (column.length >= 6) return 136
  return 112
}

/**
 * 给 Ant Design 的吸顶表头和表体同一个明确的横向宽度，避免 max-content
 * 分别按表头文字和表体数字计算，造成后续列整体错位。
 */
export function visitSummaryScrollWidth(columns: string[]): number {
  return columns.reduce((total, column) => total + visitSummaryColumnWidth(column), 0)
}
