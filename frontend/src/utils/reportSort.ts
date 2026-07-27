export type SortDirection = 'asc' | 'desc'

export interface ReportSortState {
  column: string
  direction: SortDirection
}

export function nextReportSort(
  current: ReportSortState | null,
  column: string,
): ReportSortState | null {
  if (!current || current.column !== column) return { column, direction: 'asc' }
  if (current.direction === 'asc') return { column, direction: 'desc' }
  return null
}

function isEmpty(value: unknown): boolean {
  return value == null || value === ''
}

function numericValue(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value !== 'string' || value.trim() === '') return null
  if (!/^-?\d+(?:\.\d+)?$/.test(value.trim())) return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

export function sortReportRows<T extends Record<string, any>>(
  rows: T[],
  sort: ReportSortState | null,
): T[] {
  if (!sort) return rows

  return rows
    .map((row, index) => ({ row, index }))
    .sort((left, right) => {
      const leftValue = left.row[sort.column]
      const rightValue = right.row[sort.column]
      const leftEmpty = isEmpty(leftValue)
      const rightEmpty = isEmpty(rightValue)

      if (leftEmpty !== rightEmpty) return leftEmpty ? 1 : -1
      if (leftEmpty && rightEmpty) return left.index - right.index

      const leftNumber = numericValue(leftValue)
      const rightNumber = numericValue(rightValue)
      let comparison: number
      if (leftNumber != null && rightNumber != null) {
        comparison = leftNumber - rightNumber
      } else {
        comparison = String(leftValue).localeCompare(String(rightValue), 'zh-CN', {
          numeric: true,
          sensitivity: 'base',
        })
      }

      if (comparison === 0) return left.index - right.index
      return sort.direction === 'asc' ? comparison : -comparison
    })
    .map(({ row }) => row)
}
