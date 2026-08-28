import { useEffect, useMemo, useState } from 'react'
import {
  Button,
  Drawer,
  Empty,
  Input,
  Segmented,
  Select,
  Tag,
} from 'antd'
import type { TableColumnsType, TableProps } from 'antd'
import {
  RightOutlined,
  SearchOutlined,
  SortAscendingOutlined,
  SortDescendingOutlined,
} from '@ant-design/icons'
import AppTable from './AppTable'
import { buildReportTableTotal } from '../utils/tableTotals'

type ReportRow = Record<string, any>
type SortDirection = 'asc' | 'desc'
type ViewMode = 'compact' | 'full'

interface Props {
  columns: string[]
  rows: ReportRow[]
  titleColumns: string[]
  resetKey: string
  fullColumns: TableColumnsType<ReportRow>
  fullSummary?: TableProps<ReportRow>['summary']
  rowKey: (row: ReportRow, index: number) => string | number
}

const RATE_COLUMNS = new Set(['核查完成率', '核查见底率'])
const STATUS_COLUMNS = ['未核查', '已核查', '已完成']
const PREFERRED_METRICS = [
  '核查完成率',
  '数据总数',
  '已完成',
  '已核查',
  '未核查',
  '无法见底数',
  '核查见底率',
  '每日人均核查数',
  '在岗人日',
]

function formatValue(value: unknown, column: string): string {
  if (value == null || value === '') return '-'
  if (RATE_COLUMNS.has(column)) {
    const number = Number(value)
    return Number.isFinite(number) ? `${(number * 100).toFixed(0)}%` : '-'
  }
  return String(value)
}

function numeric(value: unknown): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

function rateClass(value: unknown): string {
  const parsed = numeric(value)
  if (parsed >= 0.8) return 'text-emerald-700'
  if (parsed >= 0.5) return 'text-amber-700'
  return 'text-rose-700'
}

function entityLabels(row: ReportRow, titleColumns: string[]) {
  const values = titleColumns
    .map(column => String(row[column] || '').trim())
    .filter(Boolean)
  return {
    primary: values[values.length - 1] || '未命名',
    secondary: values.length > 1 ? values.slice(0, -1).join(' · ') : '',
  }
}

export default function MobileReportTable({
  columns,
  rows,
  titleColumns,
  resetKey,
  fullColumns,
  fullSummary,
  rowKey,
}: Props) {
  const [viewMode, setViewMode] = useState<ViewMode>('compact')
  const [keyword, setKeyword] = useState('')
  const [metric, setMetric] = useState('')
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc')
  const [selectedRow, setSelectedRow] = useState<ReportRow | null>(null)
  const [tableRows, setTableRows] = useState<ReportRow[]>(rows)

  const metricColumns = useMemo(() => {
    const candidates = columns.filter(column => (
      column !== 'id'
      && !titleColumns.includes(column)
      && rows.some(row => Number.isFinite(Number(row[column])))
    ))
    return [
      ...PREFERRED_METRICS.filter(column => candidates.includes(column)),
      ...candidates.filter(column => !PREFERRED_METRICS.includes(column)),
    ]
  }, [columns, rows, titleColumns])

  useEffect(() => {
    setViewMode('compact')
    setKeyword('')
    setSortDirection('desc')
    setSelectedRow(null)
  }, [resetKey])

  useEffect(() => {
    if (!metricColumns.includes(metric)) {
      setMetric(metricColumns[0] || '')
    }
  }, [metric, metricColumns])

  const filteredRows = useMemo(() => {
    const search = keyword.trim().toLocaleLowerCase('zh-CN')
    if (!search) return rows
    return rows.filter(row => titleColumns.some(column => (
      String(row[column] || '').toLocaleLowerCase('zh-CN').includes(search)
    )))
  }, [keyword, rows, titleColumns])

  const sortedRows = useMemo(() => {
    if (!metric) return filteredRows
    const direction = sortDirection === 'asc' ? 1 : -1
    return [...filteredRows].sort((left, right) => {
      const difference = numeric(left[metric]) - numeric(right[metric])
      if (difference !== 0) return difference * direction
      const leftLabel = entityLabels(left, titleColumns).primary
      const rightLabel = entityLabels(right, titleColumns).primary
      return leftLabel.localeCompare(rightLabel, 'zh-CN')
    })
  }, [filteredRows, metric, sortDirection, titleColumns])

  useEffect(() => {
    setTableRows(filteredRows)
  }, [filteredRows])

  const rowsForTotal = viewMode === 'full' ? tableRows : filteredRows
  const total = useMemo(
    () => buildReportTableTotal(columns, rowsForTotal),
    [columns, rowsForTotal],
  )
  const visibleStatusColumns = STATUS_COLUMNS.filter(column => columns.includes(column))
  const visibleColumns = columns.filter(column => column !== 'id')
  const fixedFullColumns = fullColumns.map((column, index) => ({
    ...column,
    fixed: index < titleColumns.length ? 'left' as const : column.fixed,
  }))

  return (
    <div className="mobile-report-table__layout">
      <section className="app-card app-card--padded space-y-3">
        <Segmented
          block
          value={viewMode}
          onChange={value => setViewMode(value as ViewMode)}
          options={[
            { label: '精简列表', value: 'compact' },
            { label: '完整表格', value: 'full' },
          ]}
        />

        <Input
          allowClear
          value={keyword}
          onChange={event => setKeyword(event.target.value)}
          prefix={<SearchOutlined className="text-slate-400" />}
          placeholder={`搜索${titleColumns.join('或')}`}
        />

        {viewMode === 'compact' && metricColumns.length > 0 && (
          <div className="flex gap-2">
            <Select
              className="min-w-0 flex-1"
              value={metric}
              onChange={setMetric}
              options={metricColumns.map(column => ({
                value: column,
                label: `重点：${column}`,
              }))}
            />
            <Button
              icon={sortDirection === 'desc'
                ? <SortDescendingOutlined />
                : <SortAscendingOutlined />}
              onClick={() => setSortDirection(current => (
                current === 'desc' ? 'asc' : 'desc'
              ))}
              aria-label={sortDirection === 'desc' ? '切换为升序' : '切换为降序'}
            >
              {sortDirection === 'desc' ? '从高到低' : '从低到高'}
            </Button>
          </div>
        )}

        <div className="flex items-center justify-between gap-3 rounded-lg bg-blue-50 px-3 py-2.5">
          <div>
            <div className="text-xs text-blue-700">筛选后总计</div>
            <div className="mt-0.5 text-sm font-medium text-blue-950">
              {rowsForTotal.length} 条
            </div>
          </div>
          {metric && (
            <div className="min-w-0 text-right">
              <div className="truncate text-xs text-blue-700">{metric}</div>
              <div className="mt-0.5 text-lg font-semibold text-blue-950">
                {formatValue(total[metric], metric)}
              </div>
            </div>
          )}
        </div>
      </section>

      {filteredRows.length === 0 ? (
        <section className="app-card py-6">
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有符合条件的数据" />
        </section>
      ) : viewMode === 'full' ? (
        <AppTable<ReportRow>
          key={`mobile-full-${resetKey}`}
          columns={fixedFullColumns}
          dataSource={filteredRows}
          rowKey={rowKey}
          reportGrid
          sticky
          summary={fullSummary}
          scroll={{ x: Math.max(visibleColumns.length * 112, 720) }}
          onChange={(_, __, ___, extra) => setTableRows(extra.currentDataSource)}
        />
      ) : (
        <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          {sortedRows.map((row, index) => {
            const labels = entityLabels(row, titleColumns)
            return (
              <button
                key={rowKey(row, index)}
                type="button"
                className="block w-full border-b border-slate-100 px-4 py-3 text-left last:border-b-0 hover:bg-slate-50 focus-visible:relative focus-visible:z-10"
                onClick={() => setSelectedRow(row)}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="truncate text-[17px] font-semibold leading-6 text-slate-900">
                      {labels.primary}
                    </div>
                    {labels.secondary && (
                      <div className="mt-0.5 truncate text-[13px] text-slate-500">
                        {labels.secondary}
                      </div>
                    )}
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <div className="text-right">
                      <div className="text-[11px] text-slate-400">{metric}</div>
                      <div className={`text-base font-semibold ${RATE_COLUMNS.has(metric) ? rateClass(row[metric]) : 'text-slate-900'}`}>
                        {formatValue(row[metric], metric)}
                      </div>
                    </div>
                    <RightOutlined className="text-xs text-slate-300" />
                  </div>
                </div>
                <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs">
                  {columns.includes('数据总数') && (
                    <span className="text-slate-600">
                      总数 <strong className="font-semibold text-slate-800">{formatValue(row.数据总数, '数据总数')}</strong>
                    </span>
                  )}
                  {visibleStatusColumns.map(column => (
                    <span key={column} className="text-slate-500">
                      {column} <strong className="font-semibold text-slate-700">{formatValue(row[column], column)}</strong>
                    </span>
                  ))}
                </div>
              </button>
            )
          })}
        </div>
      )}

      <Drawer
        placement="bottom"
        height="82vh"
        open={Boolean(selectedRow)}
        onClose={() => setSelectedRow(null)}
        title={selectedRow
          ? titleColumns.map(column => selectedRow[column]).filter(Boolean).join(' · ')
          : '统计详情'}
        extra={metric && selectedRow ? (
          <Tag color="blue">
            {metric} {formatValue(selectedRow[metric], metric)}
          </Tag>
        ) : undefined}
      >
        {selectedRow && (
          <div className="grid grid-cols-2 gap-2">
            {visibleColumns
              .filter(column => !titleColumns.includes(column))
              .map(column => (
                <div key={column} className="rounded-lg border border-slate-200 p-3">
                  <div className="text-xs text-slate-500">{column}</div>
                  <div className={`mt-1 text-base font-semibold ${RATE_COLUMNS.has(column) ? rateClass(selectedRow[column]) : 'text-slate-900'}`}>
                    {formatValue(selectedRow[column], column)}
                  </div>
                </div>
              ))}
          </div>
        )}
      </Drawer>
    </div>
  )
}
