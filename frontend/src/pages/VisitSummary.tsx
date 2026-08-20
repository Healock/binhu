import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert,
  Button,
  DatePicker,
  message,
  Modal,
  Select,
  Table,
  Tag,
} from 'antd'
import type { TableColumnsType } from 'antd'
import {
  CalendarOutlined,
  DownloadOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import { useSearchParams } from 'react-router-dom'
import AppTable from '../components/AppTable'
import DataOverview from '../components/DataOverview'
import VisitSourcePanel from '../components/VisitSourcePanel'
import { EmptyState, LoadingState, PageHeader, Panel } from '../components/ui'
import {
  formatUTCTime,
  formatDateInTimezone,
  getVisitCoverage,
  getVisitSummary,
  recordXlsxExport,
  type VisitCoverage,
  type VisitSummaryReport,
  type VisitSummaryCategory,
} from '../api/client'
import { useAuth } from '../context/AuthContext'
import { exportSummaryWorkbook } from '../utils/summaryXlsx'
import {
  visitSummaryColumnWidth,
  visitSummaryScrollWidth,
} from '../utils/summaryTableLayout'
import { buildVisitTableTotal } from '../utils/tableTotals'

const EMPTY_FILTER_VALUE = '__binhu_empty_visit_summary_value__'
const SUMMARY_RATE_COLUMNS = new Set(['星级评定率'])
const SUMMARY_DECIMAL_COLUMNS = new Set([
  '在岗人日',
  '人均日走访户数',
  '人均日变动数',
  '户均变动数',
])
type VisitSummaryRow = Record<string, string | number | null>
const VISIT_CATEGORY_OPTIONS: Array<{
  value: VisitSummaryCategory
  label: string
}> = [
  { value: 'rental', label: '出租房' },
  { value: 'self_owned', label: '自购房' },
]

function DateRange({ start, end }: { start: string | null; end: string | null }) {
  if (!start || !end) return <span className="text-slate-400">暂无数据</span>
  return <span>{start} 至 {end}</span>
}

function formatSummaryValue(value: unknown, column: string) {
  if (value == null || value === '') return column === '姓名' ? '' : '-'
  if (SUMMARY_RATE_COLUMNS.has(column)) {
    return `${(Number(value) * 100).toFixed(1)}%`
  }
  if (SUMMARY_DECIMAL_COLUMNS.has(column)) {
    return Number(value).toFixed(1)
  }
  return String(value)
}

function compareSummaryValues(left: unknown, right: unknown, sortOrder?: string | null) {
  const leftEmpty = left == null || left === ''
  const rightEmpty = right == null || right === ''
  if (leftEmpty || rightEmpty) {
    if (leftEmpty && rightEmpty) return 0
    const emptyAfter = leftEmpty ? 1 : -1
    return sortOrder === 'descend' ? -emptyAfter : emptyAfter
  }
  const leftNumber = typeof left === 'number' ? left : Number(left)
  const rightNumber = typeof right === 'number' ? right : Number(right)
  if (Number.isFinite(leftNumber) && Number.isFinite(rightNumber)) {
    return leftNumber - rightNumber
  }
  return String(left).localeCompare(String(right), 'zh-CN', {
    numeric: true,
    sensitivity: 'base',
  })
}

function visitSummaryColumns(
  columns: string[],
  rows: VisitSummaryRow[],
): TableColumnsType<VisitSummaryRow> {
  return columns.map(column => {
    const filterOptions = new Map<string, { text: string; value: string; raw: unknown }>()
    for (const row of rows) {
      const raw = row[column]
      const value = raw == null || raw === '' ? EMPTY_FILTER_VALUE : String(raw)
      if (!filterOptions.has(value)) {
        filterOptions.set(value, {
          text: formatSummaryValue(raw, column),
          value,
          raw,
        })
      }
    }
    return {
      title: column,
      dataIndex: column,
      key: column,
      width: visitSummaryColumnWidth(column),
      sorter: (left, right, sortOrder) => (
        compareSummaryValues(left[column], right[column], sortOrder)
      ),
      filters: Array.from(filterOptions.values())
        .sort((left, right) => compareSummaryValues(left.raw, right.raw))
        .map(({ text, value }) => ({ text, value })),
      filterSearch: true,
      onFilter: (selectedValue, row) => {
        const raw = row[column]
        const value = raw == null || raw === '' ? EMPTY_FILTER_VALUE : String(raw)
        return value === String(selectedValue)
      },
      render: (value: unknown) => formatSummaryValue(value, column),
    }
  })
}

function visitSummaryTotal(
  columns: string[],
) {
  return (currentRows: readonly VisitSummaryRow[]) => {
    const summary = buildVisitTableTotal(
      columns,
      currentRows,
    )
    return (
      <Table.Summary.Row className="app-report-total-row">
        {columns.map((column, index) => (
          <Table.Summary.Cell index={index} key={column}>
            <span className={index === 0 ? 'font-semibold text-blue-900' : ''}>
              {formatSummaryValue(summary[column], column)}
            </span>
          </Table.Summary.Cell>
        ))}
      </Table.Summary.Row>
    )
  }
}

function SummaryCard({
  row,
  columns,
  titleColumns,
  total = false,
}: {
  row: VisitSummaryRow
  columns: string[]
  titleColumns: string[]
  total?: boolean
}) {
  return (
    <div className={`app-card app-card--padded space-y-1.5${total ? ' app-report-total-card' : ''}`}>
      <div className="mb-1 flex items-center justify-between border-b pb-2">
        <span className="font-semibold text-gray-800">
          {titleColumns.map(column => row[column]).filter(Boolean).join(' · ')}
        </span>
      </div>
      <div className="grid grid-cols-3 gap-2 text-sm">
        {columns.filter(column => !titleColumns.includes(column)).map(column => (
          <div key={column}>
            <span className="block text-xs text-gray-400">{column}</span>
            <span className="text-gray-800">
              {formatSummaryValue(row[column], column)}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function VisitSummary() {
  const { recordActivity, systemTimezone } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const initialQuery = useRef({
    start: searchParams.get('start') || '',
    end: searchParams.get('end') || '',
    category: searchParams.get('category') || '',
    scope: searchParams.get('scope') || '',
    community: searchParams.get('community') || '',
  }).current
  const initialStart = initialQuery.start
  const initialEnd = initialQuery.end
  const initialRange = initialStart && initialEnd
    ? [initialStart, initialEnd] as [string, string]
    : null
  const initialCategory: VisitSummaryCategory = initialQuery.category === 'self_owned'
    ? 'self_owned'
    : 'rental'
  const responsibilityScope = initialQuery.scope === 'responsibility' ? 'responsibility' : 'permission'
  const requestedCommunity = initialQuery.community
  const [coverage, setCoverage] = useState<VisitCoverage | null>(null)
  const [coverageLoading, setCoverageLoading] = useState(false)
  const [coverageError, setCoverageError] = useState('')
  const [missingOpen, setMissingOpen] = useState(false)
  const [summaryRange, setSummaryRange] = useState<[string, string] | null>(initialRange)
  const [summaryCategory, setSummaryCategory] = useState<VisitSummaryCategory>(initialCategory)
  const [shownSummaryRange, setShownSummaryRange] = useState<[string, string] | null>(null)
  const [shownSummaryCategory, setShownSummaryCategory] = useState<VisitSummaryCategory | null>(null)
  const [summaryReport, setSummaryReport] = useState<VisitSummaryReport | null>(null)
  const [summaryLoading, setSummaryLoading] = useState(false)
  const [summaryError, setSummaryError] = useState('')
  const [visibleInspectorRows, setVisibleInspectorRows] = useState<VisitSummaryRow[]>([])
  const [visibleCommunityRows, setVisibleCommunityRows] = useState<VisitSummaryRow[]>([])
  const [exporting, setExporting] = useState(false)
  const rangeInitialized = useRef(Boolean(initialRange))
  const summaryRequestId = useRef(0)

  const loadSummary = useCallback(async (
    range: [string, string],
    category: VisitSummaryCategory,
  ) => {
    const requestId = summaryRequestId.current + 1
    summaryRequestId.current = requestId
    setSummaryLoading(true)
    setSummaryError('')
    try {
      const nextReport = await getVisitSummary(range[0], range[1], category, {
        scope: responsibilityScope,
        community: requestedCommunity || undefined,
      })
      if (requestId !== summaryRequestId.current) return
      setSummaryReport(nextReport)
      setShownSummaryRange(range)
      setShownSummaryCategory(category)
    } catch (error: any) {
      if (requestId !== summaryRequestId.current) return
      setSummaryError(
        error?.response?.data?.detail
          || '走访汇总读取失败，请稍后重试',
      )
    } finally {
      if (requestId === summaryRequestId.current) {
        setSummaryLoading(false)
      }
    }
  }, [requestedCommunity, responsibilityScope])

  const loadCoverage = useCallback(async () => {
    setCoverageLoading(true)
    setCoverageError('')
    try {
      const nextCoverage = await getVisitCoverage()
      setCoverage(nextCoverage)
      if (!rangeInitialized.current) {
        // 默认只看系统业务时区的当天；覆盖范围仅用于提示可用日期，
        // 不应把首次打开页面的查询范围扩展成整段历史数据。
        const fallbackDate = formatDateInTimezone(new Date(), systemTimezone)
        const initialRange: [string, string] = [
          fallbackDate,
          fallbackDate,
        ]
        rangeInitialized.current = true
        setSummaryRange(initialRange)
        await loadSummary(initialRange, initialCategory)
      } else if (initialRange) {
        await loadSummary(initialRange, initialCategory)
      }
    } catch {
      setCoverageError('走访数据范围读取失败，请稍后重试')
    } finally {
      setCoverageLoading(false)
    }
  }, [initialCategory, initialEnd, initialStart, loadSummary, systemTimezone])

  useEffect(() => {
    loadCoverage()
  }, [loadCoverage])

  useEffect(() => {
    if (!summaryRange) return
    const next = new URLSearchParams()
    next.set('start', summaryRange[0])
    next.set('end', summaryRange[1])
    next.set('category', summaryCategory)
    if (responsibilityScope === 'responsibility') next.set('scope', 'responsibility')
    if (requestedCommunity) next.set('community', requestedCommunity)
    setSearchParams(next, { replace: true })
  }, [requestedCommunity, responsibilityScope, setSearchParams, summaryCategory, summaryRange])

  const shownMissingDates = coverage?.missing_dates.slice(0, 10) || []
  const inspectorRows = (summaryReport?.inspector.data || []) as VisitSummaryRow[]
  const communityRows = (summaryReport?.community.data || []) as VisitSummaryRow[]
  const selectedStartDate = summaryRange?.[0] || ''
  const selectedEndDate = summaryRange?.[1] || ''
  const shownRangeLabel = shownSummaryRange
    ? `${shownSummaryRange[0]} 至 ${shownSummaryRange[1]}`
    : '尚未查询'
  const shownCategoryLabel = summaryReport?.category_label
    || VISIT_CATEGORY_OPTIONS.find(
      option => option.value === shownSummaryCategory,
    )?.label
    || '出租房'
  const shownResultLabel = `${shownCategoryLabel} · ${shownRangeLabel}`

  useEffect(() => {
    setVisibleInspectorRows(inspectorRows)
    setVisibleCommunityRows(communityRows)
  }, [summaryReport])

  const handleExport = async () => {
    if (!summaryReport || !shownSummaryRange) return
    setExporting(true)
    try {
      await recordActivity()
      await recordXlsxExport({
        export_type: 'visit_summary',
        start_date: shownSummaryRange[0],
        end_date: shownSummaryRange[1],
        summary_type: shownCategoryLabel,
        inspector_rows: visibleInspectorRows.length,
        community_rows: visibleCommunityRows.length,
      })
      await exportSummaryWorkbook({
        fileName: `走访汇总_${shownCategoryLabel}_${shownSummaryRange[0]}_至_${shownSummaryRange[1]}`,
        tables: [
          {
            sheet: '人员汇总',
            columns: summaryReport.inspector.columns,
            rows: visibleInspectorRows,
            total: buildVisitTableTotal(
              summaryReport.inspector.columns,
              visibleInspectorRows,
            ),
          },
          {
            sheet: '社区汇总',
            columns: summaryReport.community.columns,
            rows: visibleCommunityRows,
            total: buildVisitTableTotal(
              summaryReport.community.columns,
              visibleCommunityRows,
            ),
          },
        ],
      })
      message.success('已导出当前走访汇总数据')
    } catch (error) {
      message.error(error instanceof Error ? error.message : '导出失败，请稍后重试')
    } finally {
      setExporting(false)
    }
  }

  return (
    <div className="app-page min-w-0">
      <PageHeader
        title="走访汇总"
        description="按日期查看走访、信息变动和星级评定结果"
      />

      <VisitSourcePanel />

      <Panel
        title="数据库覆盖情况"
        description="用于判断下次需要补充或更新哪个日期的数据"
        extra={
          <Button
            icon={<CalendarOutlined />}
            loading={coverageLoading}
            onClick={loadCoverage}
          >
            刷新范围
          </Button>
        }
      >
        {coverageError && <Alert className="mb-4" type="error" showIcon message={coverageError} />}
        {coverage?.scope_message && (
          <Alert className="mb-4" type="info" showIcon message={coverage.scope_message} />
        )}
        <DataOverview
          loading={coverageLoading}
          rangeTitle="数据库日期范围"
          rangeValue={(
            <DateRange
              start={coverage?.start_date || null}
              end={coverage?.end_date || null}
            />
          )}
          rangeDescription="重复区间导入会自动合并去重"
          metrics={[
            {
              key: 'data-days',
              title: '有数据日期',
              value: coverage?.data_days || 0,
              suffix: '天',
            },
            {
              key: 'missing-days',
              title: '无数据日期',
              value: coverage?.missing_date_count || 0,
              suffix: '天',
            },
          ]}
        />
        <div className="mt-4 flex flex-wrap items-center gap-x-5 gap-y-2 text-sm text-slate-500">
          <span>最近走访导入：{formatUTCTime(coverage?.last_detail_import_at, systemTimezone)}</span>
          <span>最近星级导入：{formatUTCTime(coverage?.last_rating_import_at, systemTimezone)}</span>
        </div>
        {(coverage?.missing_date_count || 0) > 0 && (
          <div className="mt-3 flex flex-wrap items-center gap-2 text-sm text-slate-500">
            <span>无数据日期：</span>
            {shownMissingDates.map(item => <Tag key={item}>{item}</Tag>)}
            {(coverage?.missing_date_count || 0) > 10 && (
              <Button type="link" size="small" onClick={() => setMissingOpen(true)}>
                查看全部
              </Button>
            )}
          </div>
        )}
      </Panel>

      <Panel
        title="走访数据汇总"
        description="按入户业务日期统计；社区使用走访记录中的实际走访社区"
      >
        <div className="flex min-w-0 flex-col gap-2.5 md:flex-row md:items-center">
          <Select<VisitSummaryCategory>
            size="large"
            value={summaryCategory}
            options={VISIT_CATEGORY_OPTIONS}
            className="w-full md:w-36"
            onChange={(category) => {
              setSummaryCategory(category)
              if (summaryRange) {
                void loadSummary(summaryRange, category)
              }
            }}
          />
          <div className="flex w-full items-center gap-1.5 md:hidden">
            <input
              type="date"
              value={selectedStartDate}
              onChange={(event) => {
                const nextStart = event.target.value
                if (!nextStart) return
                setSummaryRange([
                  nextStart,
                  nextStart > selectedEndDate ? nextStart : selectedEndDate,
                ])
              }}
              className="min-h-11 min-w-0 flex-1 rounded border border-gray-300 px-2 text-sm"
            />
            <span className="text-xs text-gray-400">至</span>
            <input
              type="date"
              value={selectedEndDate}
              onChange={(event) => {
                const nextEnd = event.target.value
                if (!nextEnd) return
                setSummaryRange([
                  nextEnd < selectedStartDate ? nextEnd : selectedStartDate,
                  nextEnd,
                ])
              }}
              className="min-h-11 min-w-0 flex-1 rounded border border-gray-300 px-2 text-sm"
            />
          </div>
          <div className="hidden w-[300px] md:block">
            <DatePicker.RangePicker
              size="large"
              className="w-full"
              value={summaryRange
                ? [dayjs(summaryRange[0]), dayjs(summaryRange[1])]
                : null}
              onChange={(_, dateStrings) => {
                if (dateStrings[0] && dateStrings[1]) {
                  setSummaryRange([dateStrings[0], dateStrings[1]])
                }
              }}
              allowClear={false}
            />
          </div>
          <Button
            type="primary"
            size="large"
            icon={<SearchOutlined />}
            loading={summaryLoading}
            disabled={!summaryRange}
            onClick={() => (
              summaryRange && loadSummary(summaryRange, summaryCategory)
            )}
          >
            查询汇总
          </Button>
          <Button
            size="large"
            icon={<DownloadOutlined />}
            loading={exporting}
            disabled={!summaryReport || summaryLoading}
            onClick={handleExport}
            className="w-full md:w-auto"
          >
            导出 XLSX
          </Button>
          <span className="text-sm text-slate-500 md:ml-auto">
            当前结果：{shownResultLabel}
          </span>
        </div>
        <p className="mt-2 text-xs text-slate-500">
          出租房按系统设置中的岗位统计；自购房固定统计“自购房”岗位。社区人均值按区间内实际在岗人日计算，请假和周末休息不进入分母。
        </p>
        {summaryReport && (
          <div className="mt-3 border-t border-slate-100 pt-3">
            <div className="mb-2 text-sm font-medium text-slate-700">
              当前查询概览
            </div>
            <DataOverview
              loading={summaryLoading}
              metrics={[
                {
                  key: 'visits',
                  title: '走访户数',
                  value: summaryReport.overview.visit_records,
                  suffix: '户',
                },
                {
                  key: 'participants',
                  title: '参与人员',
                  value: summaryReport.overview.participant_count,
                  suffix: '人',
                },
                {
                  key: 'person-days',
                  title: '在岗人日',
                  value: summaryReport.overview.person_days,
                  suffix: '人日',
                  help: '把区间内每天实际在岗人数相加',
                },
                {
                  key: 'changes',
                  title: '总变动数',
                  value: summaryReport.overview.total_changes,
                  suffix: '项',
                  help: `新增 ${summaryReport.overview.added_count}、变更 ${summaryReport.overview.changed_count}、注销 ${summaryReport.overview.cancelled_count}`,
                },
                {
                  key: 'ratings',
                  title: '星级评定',
                  value: summaryReport.overview.rated_records,
                  suffix: '户',
                  help: `评定率 ${(summaryReport.overview.rating_rate * 100).toFixed(1)}%`,
                  valueStyle: { color: '#047857' },
                },
                {
                  key: 'unrated',
                  title: '仅走访未评定',
                  value: summaryReport.overview.unrated_records,
                  suffix: '户',
                  valueStyle: { color: '#d97706' },
                },
              ]}
            />
          </div>
        )}
      </Panel>

      {summaryError && (
        <Alert type="error" showIcon message={summaryError} />
      )}
      {summaryReport?.scope_message && (
        <Alert type="info" showIcon message={summaryReport.scope_message} />
      )}
      {summaryReport && summaryReport.attendance.worked_while_off > 0 && (
        <Alert
          type="info"
          showIcon
          message={`发现 ${summaryReport.attendance.worked_while_off} 个人日虽排休或请假但有真实走访，系统已按实际出勤计入`}
        />
      )}

      {summaryLoading && !summaryReport ? (
        <Panel>
          <LoadingState label="正在计算走访汇总..." />
        </Panel>
      ) : summaryReport
        && inspectorRows.length === 0
        && communityRows.length === 0 ? (
        <Panel>
          <EmptyState label={`${shownResultLabel} 暂无走访数据`} />
        </Panel>
      ) : summaryReport ? (
        <>
          <div className="space-y-6 md:hidden">
            <div className="space-y-3">
              <h2 className="px-1 text-sm font-semibold text-gray-700">
                人员汇总（{inspectorRows.length} 行）
                <span className="ml-2 font-normal text-gray-400">{shownResultLabel}</span>
              </h2>
              <div className="grid grid-cols-1 gap-3">
                {inspectorRows.map((row, index) => (
                  <SummaryCard
                    key={`${row.社区}-${row.姓名}-${index}`}
                    row={row}
                    columns={summaryReport.inspector.columns}
                    titleColumns={['社区', '姓名']}
                  />
                ))}
                <SummaryCard
                  row={summaryReport.inspector.summary as VisitSummaryRow}
                  columns={summaryReport.inspector.columns}
                  titleColumns={['社区', '姓名']}
                  total
                />
              </div>
            </div>
            <div className="space-y-3">
              <h2 className="px-1 text-sm font-semibold text-gray-700">
                社区汇总（{communityRows.length} 个社区）
                <span className="ml-2 font-normal text-gray-400">{shownResultLabel}</span>
              </h2>
              <div className="grid grid-cols-1 gap-3">
                {communityRows.map((row, index) => (
                  <SummaryCard
                    key={`${row.社区}-${index}`}
                    row={row}
                    columns={summaryReport.community.columns}
                    titleColumns={['社区']}
                  />
                ))}
                <SummaryCard
                  row={summaryReport.community.summary as VisitSummaryRow}
                  columns={summaryReport.community.columns}
                  titleColumns={['社区']}
                  total
                />
              </div>
            </div>
          </div>
          <div className="hidden space-y-6 md:block">
            <AppTable<VisitSummaryRow>
              key={`visit-inspector-${shownSummaryCategory}-${shownRangeLabel}`}
              columns={visitSummaryColumns(
                summaryReport.inspector.columns,
                inspectorRows,
              )}
              dataSource={inspectorRows}
              reportGrid
              rowKey={(row, index) => `${row.社区}-${row.姓名}-${index}`}
              loading={summaryLoading}
              scroll={{ x: visitSummaryScrollWidth(summaryReport.inspector.columns) }}
              sticky
              summary={visitSummaryTotal(
                summaryReport.inspector.columns,
              )}
              onChange={(_, __, ___, extra) => {
                setVisibleInspectorRows([...extra.currentDataSource])
              }}
              title={currentRows => (
                <h2 className="text-sm font-semibold text-gray-700">
                  人员汇总（{currentRows.length} 行）
                  <span className="ml-2 font-normal text-gray-400">{shownResultLabel}</span>
                </h2>
              )}
            />
            <AppTable<VisitSummaryRow>
              key={`visit-community-${shownSummaryCategory}-${shownRangeLabel}`}
              columns={visitSummaryColumns(
                summaryReport.community.columns,
                communityRows,
              )}
              dataSource={communityRows}
              reportGrid
              rowKey={(row, index) => `${row.社区}-${index}`}
              loading={summaryLoading}
              scroll={{ x: visitSummaryScrollWidth(summaryReport.community.columns) }}
              sticky
              summary={visitSummaryTotal(
                summaryReport.community.columns,
              )}
              onChange={(_, __, ___, extra) => {
                setVisibleCommunityRows([...extra.currentDataSource])
              }}
              title={currentRows => (
                <h2 className="text-sm font-semibold text-gray-700">
                  社区汇总（{currentRows.length} 个社区）
                  <span className="ml-2 font-normal text-gray-400">{shownResultLabel}</span>
                </h2>
              )}
            />
          </div>
        </>
      ) : null}

      <Modal
        open={missingOpen}
        title={`无数据日期（${coverage?.missing_date_count || 0} 天）`}
        footer={null}
        onCancel={() => setMissingOpen(false)}
      >
        <div className="flex max-h-[55vh] flex-wrap gap-2 overflow-y-auto py-2">
          {coverage?.missing_dates.map(item => <Tag key={item}>{item}</Tag>)}
        </div>
      </Modal>
    </div>
  )
}
