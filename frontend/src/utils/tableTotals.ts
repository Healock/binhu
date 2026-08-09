export type SummaryRow = Record<string, string | number | null | undefined>

const REPORT_SUM_COLUMNS = new Set([
  '数据总数',
  '未核查',
  '已核查',
  '已完成',
  '无法见底数',
  '网格员人数',
  '在岗人日',
])

const VISIT_SUM_COLUMNS = new Set([
  '走访户数',
  '网格员人数',
  '新增',
  '变更',
  '注销',
  '总变动数',
  '星级评定数',
  '在岗人日',
])

function numeric(value: unknown): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

function sumColumn(rows: readonly SummaryRow[], column: string): number {
  return rows.reduce((total, row) => total + numeric(row[column]), 0)
}

function roundRatio(
  numerator: number,
  denominator: number,
  precision: number,
): number {
  if (denominator <= 0) return 0
  const factor = 10 ** precision
  return Math.round((numerator / denominator + Number.EPSILON) * factor) / factor
}

function buildLabels(columns: readonly string[]): SummaryRow {
  const total: SummaryRow = {}
  let labelWritten = false
  for (const column of columns) {
    if (column === '社区' || column === '姓名') {
      total[column] = labelWritten ? '' : '总计'
      labelWritten = true
    }
  }
  return total
}

export function buildReportTableTotal(
  columns: readonly string[],
  rows: readonly SummaryRow[],
): SummaryRow {
  const total = buildLabels(columns)
  for (const column of columns) {
    if (REPORT_SUM_COLUMNS.has(column)) {
      total[column] = sumColumn(rows, column)
    }
  }

  const dataTotal = numeric(total['数据总数'])
  const completed = columns.includes('已完成')
    ? numeric(total['已完成'])
    : numeric(total['已核查'])
  const unable = numeric(total['无法见底数'])
  const personDays = columns.includes('在岗人日')
    ? numeric(total['在岗人日'])
    : numeric(total['网格员人数'])

  if (columns.includes('核查完成率')) {
    total['核查完成率'] = roundRatio(completed, dataTotal, 2)
  }
  if (columns.includes('核查见底率')) {
    total['核查见底率'] = roundRatio(completed, completed + unable, 2)
  }
  const averageColumn = columns.includes('每日人均核查数')
    ? '每日人均核查数'
    : columns.includes('当日人均核查数')
      ? '当日人均核查数'
      : ''
  if (averageColumn) {
    total[averageColumn] = rows.some(row => row[averageColumn] == null)
      ? null
      : roundRatio(
          rows.reduce((sum, row) => (
            sum
            + numeric(row[averageColumn])
              * numeric(row['在岗人日'] ?? row['网格员人数'])
          ), 0),
          personDays,
          2,
        )
  }
  return total
}

export function buildVisitTableTotal(
  columns: readonly string[],
  rows: readonly SummaryRow[],
  memberCount = 0,
): SummaryRow {
  const total = buildLabels(columns)
  for (const column of columns) {
    if (VISIT_SUM_COLUMNS.has(column)) {
      total[column] = sumColumn(rows, column)
    }
  }

  const visits = numeric(total['走访户数'])
  const totalChanges = (
    numeric(total['新增'])
    + numeric(total['变更'])
    + numeric(total['注销'])
  )
  const ratings = numeric(total['星级评定数'])
  total['总变动数'] = totalChanges

  if (columns.includes('户均变动数')) {
    total['户均变动数'] = roundRatio(totalChanges, visits, 1)
  }
  if (columns.includes('星级评定率')) {
    total['星级评定率'] = roundRatio(ratings, visits, 4)
  }
  if (columns.includes('人均走访户数')) {
    total['人均走访户数'] = roundRatio(visits, memberCount, 1)
  }
  if (columns.includes('人均变动数')) {
    total['人均变动数'] = roundRatio(totalChanges, memberCount, 1)
  }
  const attendanceIncomplete = rows.some(row => (
    row['人均日走访户数'] == null
    || row['人均日变动数'] == null
  ))
  const hasExactPersonDays = rows.some(row => (
    row['_person_days_exact'] != null
  ))
  const personDays = hasExactPersonDays
    ? sumColumn(rows, '_person_days_exact')
    : numeric(total['在岗人日'])
  if (columns.includes('在岗人日')) {
    total['在岗人日'] = roundRatio(personDays, 1, 1)
  }
  if (columns.includes('人均日走访户数')) {
    total['人均日走访户数'] = attendanceIncomplete
      ? null
      : roundRatio(visits, personDays, 1)
  }
  if (columns.includes('人均日变动数')) {
    total['人均日变动数'] = attendanceIncomplete
      ? null
      : roundRatio(totalChanges, personDays, 1)
  }
  return total
}
