import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert,
  Button,
  DatePicker,
  Modal,
  Select,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Upload,
} from 'antd'
import type { TableColumnsType, UploadFile, UploadProps } from 'antd'
import {
  CalendarOutlined,
  InboxOutlined,
  SearchOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import AppTable from '../components/AppTable'
import { EmptyState, LoadingState, PageHeader, Panel } from '../components/ui'
import { useAuth } from '../context/AuthContext'
import {
  formatUTCTime,
  getVisitCoverage,
  getVisitImportIssues,
  getVisitSummary,
  uploadStarRating,
  uploadVisitDetail,
  type VisitCoverage,
  type VisitImportIssue,
  type VisitImportResult,
  type VisitSummaryReport,
  type VisitSummaryCategory,
} from '../api/client'
import { buildVisitTableTotal } from '../utils/tableTotals'

const { Dragger } = Upload
const MAX_FILE_BYTES = 20 * 1024 * 1024
const ISSUE_PAGE_SIZE = 50
const EMPTY_FILTER_VALUE = '__binhu_empty_visit_summary_value__'
const SUMMARY_RATE_COLUMNS = new Set(['星级评定率'])
const SUMMARY_DECIMAL_COLUMNS = new Set([
  '人均走访户数',
  '人均变动数',
  '户均变动数',
])
type VisitSummaryRow = Record<string, string | number>
const VISIT_CATEGORY_OPTIONS: Array<{
  value: VisitSummaryCategory
  label: string
}> = [
  { value: 'rental', label: '出租房' },
  { value: 'self_owned', label: '自购房' },
]

const statusMeta = {
  success: { color: 'success', label: '导入成功' },
  partial: { color: 'warning', label: '部分成功' },
  failed: { color: 'error', label: '导入失败' },
  duplicate: { color: 'default', label: '文件已导入' },
} as const

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
      width: column === '社区' || column === '姓名' ? 120 : 112,
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
  memberCount?: (rows: readonly VisitSummaryRow[]) => number,
) {
  return (currentRows: readonly VisitSummaryRow[]) => {
    const summary = buildVisitTableTotal(
      columns,
      currentRows,
      memberCount?.(currentRows) || 0,
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
  const { user } = useAuth()
  const canUpload = user?.role === 'super_admin' || user?.role === 'admin'
  const [coverage, setCoverage] = useState<VisitCoverage | null>(null)
  const [coverageLoading, setCoverageLoading] = useState(false)
  const [coverageError, setCoverageError] = useState('')
  const [missingOpen, setMissingOpen] = useState(false)
  const [fileList, setFileList] = useState<UploadFile[]>([])
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [importing, setImporting] = useState(false)
  const [importError, setImportError] = useState('')
  const [ratingFileList, setRatingFileList] = useState<UploadFile[]>([])
  const [selectedRatingFile, setSelectedRatingFile] = useState<File | null>(null)
  const [ratingImporting, setRatingImporting] = useState(false)
  const [ratingImportError, setRatingImportError] = useState('')
  const [result, setResult] = useState<VisitImportResult | null>(null)
  const [issues, setIssues] = useState<VisitImportIssue[]>([])
  const [issueTotal, setIssueTotal] = useState(0)
  const [issuePage, setIssuePage] = useState(1)
  const [issueLoading, setIssueLoading] = useState(false)
  const [summaryRange, setSummaryRange] = useState<[string, string] | null>(null)
  const [summaryCategory, setSummaryCategory] = useState<VisitSummaryCategory>('rental')
  const [shownSummaryRange, setShownSummaryRange] = useState<[string, string] | null>(null)
  const [shownSummaryCategory, setShownSummaryCategory] = useState<VisitSummaryCategory | null>(null)
  const [summaryReport, setSummaryReport] = useState<VisitSummaryReport | null>(null)
  const [summaryLoading, setSummaryLoading] = useState(false)
  const [summaryError, setSummaryError] = useState('')
  const rangeInitialized = useRef(false)
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
      const nextReport = await getVisitSummary(range[0], range[1], category)
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
  }, [])

  const loadCoverage = useCallback(async () => {
    setCoverageLoading(true)
    setCoverageError('')
    try {
      const nextCoverage = await getVisitCoverage()
      setCoverage(nextCoverage)
      if (!rangeInitialized.current) {
        const fallbackDate = dayjs().format('YYYY-MM-DD')
        const initialRange: [string, string] = [
          nextCoverage.start_date || fallbackDate,
          nextCoverage.end_date || fallbackDate,
        ]
        rangeInitialized.current = true
        setSummaryRange(initialRange)
        await loadSummary(initialRange, 'rental')
      }
    } catch {
      setCoverageError('走访数据范围读取失败，请稍后重试')
    } finally {
      setCoverageLoading(false)
    }
  }, [loadSummary])

  useEffect(() => {
    loadCoverage()
  }, [loadCoverage])

  const beforeUpload: UploadProps['beforeUpload'] = file => {
    setImportError('')
    setResult(null)
    setSelectedFile(null)
    setFileList([])
    setIssues([])
    setIssueTotal(0)
    if (!file.name.toLowerCase().endsWith('.xlsx')) {
      setImportError('只支持 .xlsx 文件')
      return Upload.LIST_IGNORE
    }
    if (file.size > MAX_FILE_BYTES) {
      setImportError('XLSX 文件不能超过 20MB')
      return Upload.LIST_IGNORE
    }
    setSelectedFile(file)
    setFileList([{
      uid: file.uid,
      name: file.name,
      size: file.size,
      type: file.type,
      status: 'done',
      originFileObj: file,
    }])
    return false
  }

  const handleImport = async () => {
    if (!selectedFile) return
    setImporting(true)
    setImportError('')
    try {
      const nextResult = await uploadVisitDetail(selectedFile)
      setResult(nextResult)
      setCoverage(nextResult.coverage)
      setIssues(nextResult.issues.data)
      setIssueTotal(nextResult.issues.total)
      setIssuePage(1)
      if (shownSummaryRange) {
        const previouslyCoveredAllDates = (
          shownSummaryRange[0] === coverage?.start_date
          && shownSummaryRange[1] === coverage?.end_date
        )
        const refreshedRange: [string, string] = previouslyCoveredAllDates
          && nextResult.coverage.start_date
          && nextResult.coverage.end_date
          ? [
              nextResult.coverage.start_date,
              nextResult.coverage.end_date,
            ]
          : shownSummaryRange
        setSummaryRange(refreshedRange)
        await loadSummary(
          refreshedRange,
          shownSummaryCategory || summaryCategory,
        )
      }
    } catch (error: any) {
      setImportError(
        error?.response?.data?.detail
          || (error?.code === 'ECONNABORTED' ? '导入处理超时，请稍后确认数据范围' : '上传失败，请稍后重试'),
      )
    } finally {
      setImporting(false)
    }
  }

  const beforeRatingUpload: UploadProps['beforeUpload'] = file => {
    setRatingImportError('')
    setResult(null)
    setSelectedRatingFile(null)
    setRatingFileList([])
    setIssues([])
    setIssueTotal(0)
    if (!file.name.toLowerCase().endsWith('.xlsx')) {
      setRatingImportError('只支持 .xlsx 文件')
      return Upload.LIST_IGNORE
    }
    if (file.size > MAX_FILE_BYTES) {
      setRatingImportError('XLSX 文件不能超过 20MB')
      return Upload.LIST_IGNORE
    }
    setSelectedRatingFile(file)
    setRatingFileList([{
      uid: file.uid,
      name: file.name,
      size: file.size,
      type: file.type,
      status: 'done',
      originFileObj: file,
    }])
    return false
  }

  const handleRatingImport = async () => {
    if (!selectedRatingFile) return
    setRatingImporting(true)
    setRatingImportError('')
    try {
      const nextResult = await uploadStarRating(selectedRatingFile)
      setResult(nextResult)
      setCoverage(nextResult.coverage)
      setIssues(nextResult.issues.data)
      setIssueTotal(nextResult.issues.total)
      setIssuePage(1)
      if (shownSummaryRange) {
        const previouslyCoveredAllDates = (
          shownSummaryRange[0] === coverage?.start_date
          && shownSummaryRange[1] === coverage?.end_date
        )
        const refreshedRange: [string, string] = previouslyCoveredAllDates
          && nextResult.coverage.start_date
          && nextResult.coverage.end_date
          ? [
              nextResult.coverage.start_date,
              nextResult.coverage.end_date,
            ]
          : shownSummaryRange
        setSummaryRange(refreshedRange)
        await loadSummary(
          refreshedRange,
          shownSummaryCategory || summaryCategory,
        )
      }
    } catch (error: any) {
      setRatingImportError(
        error?.response?.data?.detail
          || (error?.code === 'ECONNABORTED' ? '导入处理超时，请稍后刷新查看' : '上传失败，请稍后重试'),
      )
    } finally {
      setRatingImporting(false)
    }
  }

  const loadIssuePage = async (page: number) => {
    if (!result || (result.status === 'duplicate' && issueTotal === 0)) return
    setIssueLoading(true)
    try {
      const response = await getVisitImportIssues(result.batch_id, page, ISSUE_PAGE_SIZE)
      setIssues(response.data)
      setIssueTotal(response.total)
      setIssuePage(page)
    } catch {
      setImportError('导入问题明细读取失败，请稍后重试')
    } finally {
      setIssueLoading(false)
    }
  }

  const detailIssueColumns: TableColumnsType<VisitImportIssue> = [
    {
      title: '类型',
      dataIndex: 'severity',
      width: 90,
      render: value => (
        <Tag color={value === 'error' ? 'error' : 'warning'}>
          {value === 'error' ? '错误' : '提醒'}
        </Tag>
      ),
    },
    {
      title: 'Excel 行',
      dataIndex: 'row_number',
      width: 100,
      render: value => value || '-',
    },
    {
      title: '原因',
      dataIndex: 'message',
      width: 300,
      render: value => <span className="text-slate-700">{value}</span>,
    },
    {
      title: '社区',
      render: (_, item) => item.row_preview['村社区'] || '-',
      width: 130,
    },
    {
      title: '操作人',
      render: (_, item) => item.row_preview['操作人'] || '-',
      width: 110,
    },
    {
      title: '入户时间',
      render: (_, item) => item.row_preview['入户时间'] || '-',
      width: 170,
    },
    {
      title: '地址',
      width: 280,
      ellipsis: { showTitle: false },
      render: (_, item) => (
        <Tooltip title={item.row_preview['地址'] || '-'}>
          <span>{item.row_preview['地址'] || '-'}</span>
        </Tooltip>
      ),
    },
    {
      title: '操作人账号',
      render: (_, item) => item.row_preview['操作人账号'] || '-',
      width: 180,
    },
  ]

  const ratingIssueColumns: TableColumnsType<VisitImportIssue> = [
    {
      title: '类型',
      dataIndex: 'severity',
      width: 90,
      render: value => (
        <Tag color={value === 'error' ? 'error' : 'warning'}>
          {value === 'error' ? '错误' : '提醒'}
        </Tag>
      ),
    },
    {
      title: 'Excel 行',
      dataIndex: 'row_number',
      width: 100,
      render: value => value || '-',
    },
    {
      title: '原因',
      dataIndex: 'message',
      width: 340,
      render: value => <span className="text-slate-700">{value}</span>,
    },
    {
      title: '社区',
      width: 130,
      render: (_, item) => item.row_preview['所属社区'] || '-',
    },
    {
      title: '星级',
      width: 130,
      render: (_, item) => item.row_preview['星级'] || '-',
    },
    {
      title: '得分',
      width: 100,
      render: (_, item) => item.row_preview['得分'] || '-',
    },
    {
      title: '采集时间',
      width: 180,
      render: (_, item) => item.row_preview['采集时间'] || '-',
    },
    {
      title: '地址',
      width: 300,
      ellipsis: { showTitle: false },
      render: (_, item) => (
        <Tooltip title={item.row_preview['地址'] || '-'}>
          <span>{item.row_preview['地址'] || '-'}</span>
        </Tooltip>
      ),
    },
  ]

  const shownMissingDates = coverage?.missing_dates.slice(0, 10) || []
  const cardMode = user?.table_display_mode === 'card'
  const inspectorRows = (summaryReport?.inspector.data || []) as VisitSummaryRow[]
  const communityRows = (summaryReport?.community.data || []) as VisitSummaryRow[]
  const countCommunityMembers = useCallback((
    visibleRows: readonly VisitSummaryRow[],
  ) => {
    const visibleCommunities = new Set(
      visibleRows.map(row => String(row.社区 || '未分配社区')),
    )
    return new Set(
      inspectorRows
        .filter(row => visibleCommunities.has(String(row.社区 || '未分配社区')))
        .map(row => String(row.姓名 || '未填写姓名')),
    ).size
  }, [inspectorRows])
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

  return (
    <div className="app-page min-w-0">
      <PageHeader
        title="走访汇总"
        description="分别上传走访明细和星级评定，系统按地址和前后 24 小时自动关联"
      />

      <Panel
        title="当前数据库数据范围"
        description="上传前先确认现有日期范围，重叠日期会自动合并去重"
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
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-7">
          <div className="rounded-lg border border-slate-200 bg-slate-50 p-4 xl:col-span-2">
            <div className="text-xs text-slate-500">已入库日期范围</div>
            <div className="mt-2 text-lg font-semibold text-slate-900">
              <DateRange start={coverage?.start_date || null} end={coverage?.end_date || null} />
            </div>
          </div>
          <div className="rounded-lg border border-slate-200 p-4">
            <Statistic title="有效走访记录" value={coverage?.total_records || 0} suffix="条" />
          </div>
          <div className="rounded-lg border border-slate-200 p-4">
            <Statistic title="已星级评定" value={coverage?.rated_records || 0} suffix="条" />
          </div>
          <div className="rounded-lg border border-slate-200 p-4">
            <Statistic title="仅走访未评定" value={coverage?.unrated_records || 0} suffix="条" />
          </div>
          <div className="rounded-lg border border-slate-200 p-4">
            <Statistic title="有数据日期" value={coverage?.data_days || 0} suffix="天" />
          </div>
          <div className="rounded-lg border border-slate-200 p-4">
            <Statistic title="无数据日期" value={coverage?.missing_date_count || 0} suffix="天" />
          </div>
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-2 text-sm text-slate-500">
          <span>最近走访导入：{formatUTCTime(coverage?.last_detail_import_at)}</span>
          <span>最近星级导入：{formatUTCTime(coverage?.last_rating_import_at)}</span>
          {shownMissingDates.map(item => <Tag key={item}>{item}</Tag>)}
          {(coverage?.missing_date_count || 0) > 10 && (
            <Button type="link" size="small" onClick={() => setMissingOpen(true)}>
              查看全部
            </Button>
          )}
        </div>
      </Panel>

      <Panel
        title="走访数据汇总"
        description="按入户业务日期统计；社区使用走访记录中的实际走访社区"
      >
        <div className="flex min-w-0 flex-col gap-3 md:flex-row md:items-center">
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
          <span className="text-sm text-slate-500 md:ml-auto">
            当前结果：{shownResultLabel}
          </span>
        </div>
        <p className="mt-3 text-xs text-slate-500">
          出租房按系统设置中的岗位统计；自购房固定统计“自购房”岗位。走访户数按去重后的走访记录计算。
        </p>
      </Panel>

      {summaryError && (
        <Alert type="error" showIcon message={summaryError} />
      )}

      {summaryLoading && !summaryReport ? (
        <Panel>
          <LoadingState label="正在计算走访汇总..." />
        </Panel>
      ) : summaryReport && inspectorRows.length === 0 ? (
        <Panel>
          <EmptyState label={`${shownResultLabel} 暂无走访数据`} />
        </Panel>
      ) : summaryReport && cardMode ? (
        <div className="space-y-6">
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
      ) : summaryReport ? (
        <>
          <AppTable<VisitSummaryRow>
            key={`visit-inspector-${shownSummaryCategory}-${shownRangeLabel}`}
            columns={visitSummaryColumns(
              summaryReport.inspector.columns,
              inspectorRows,
            )}
            dataSource={inspectorRows}
            rowKey={(row, index) => `${row.社区}-${row.姓名}-${index}`}
            loading={summaryLoading}
            sticky
            summary={visitSummaryTotal(
              summaryReport.inspector.columns,
            )}
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
            rowKey={(row, index) => `${row.社区}-${index}`}
            loading={summaryLoading}
            sticky
            summary={visitSummaryTotal(
              summaryReport.community.columns,
              countCommunityMembers,
            )}
            title={currentRows => (
              <h2 className="text-sm font-semibold text-gray-700">
                社区汇总（{currentRows.length} 个社区）
                <span className="ml-2 font-normal text-gray-400">{shownResultLabel}</span>
              </h2>
            )}
          />
        </>
      ) : null}

      <div className="grid min-w-0 gap-4 xl:grid-cols-2">
        <Panel
          title="上传走访明细"
          description="先导入走访明细；重叠日期会自动去重"
        >
          {!canUpload ? (
            <Alert
              type="info"
              showIcon
              message="只有超级管理员和管理员可以上传"
              description="你仍然可以查看上方数据库日期范围。"
            />
          ) : (
            <>
              <Dragger
                accept=".xlsx"
                maxCount={1}
                fileList={fileList}
                beforeUpload={beforeUpload}
                onRemove={() => {
                  setSelectedFile(null)
                  setFileList([])
                  setResult(null)
                  setIssues([])
                  setIssueTotal(0)
                }}
                disabled={importing || ratingImporting}
              >
                <p className="ant-upload-drag-icon"><InboxOutlined /></p>
                <p className="ant-upload-text">拖入走访明细，或点击选择 XLSX</p>
                <p className="ant-upload-hint">同一网格员同日同地址取时间最晚的一条，不同网格员分别保留。</p>
              </Dragger>
              <div className="mt-4 flex justify-end">
                <Button
                  type="primary"
                  icon={<UploadOutlined />}
                  loading={importing}
                  disabled={!selectedFile || ratingImporting}
                  onClick={handleImport}
                >
                  {importing ? '正在校验并入库' : '导入走访明细'}
                </Button>
              </div>
            </>
          )}
          {importError && <Alert className="mt-4" type="error" showIcon message={importError} />}
        </Panel>

        <Panel
          title="上传星级评定"
          description="按地址匹配采集时间前后 24 小时内最接近的走访"
        >
          {!canUpload ? (
            <Alert
              type="info"
              showIcon
              message="只有超级管理员和管理员可以上传"
              description="你仍然可以查看上方评定数量。"
            />
          ) : (
            <>
              <Dragger
                accept=".xlsx"
                maxCount={1}
                fileList={ratingFileList}
                beforeUpload={beforeRatingUpload}
                onRemove={() => {
                  setSelectedRatingFile(null)
                  setRatingFileList([])
                  setResult(null)
                  setIssues([])
                  setIssueTotal(0)
                }}
                disabled={ratingImporting || importing}
              >
                <p className="ant-upload-drag-icon"><InboxOutlined /></p>
                <p className="ant-upload-text">拖入星级评定，或点击选择 XLSX</p>
                <p className="ant-upload-hint">星级评定必须匹配已有走访；无法判断时不会强行关联。</p>
              </Dragger>
              <div className="mt-4 flex justify-end">
                <Button
                  type="primary"
                  icon={<UploadOutlined />}
                  loading={ratingImporting}
                  disabled={!selectedRatingFile || importing}
                  onClick={handleRatingImport}
                >
                  {ratingImporting ? '正在匹配并关联' : '导入星级评定'}
                </Button>
              </div>
            </>
          )}
          {ratingImportError && <Alert className="mt-4" type="error" showIcon message={ratingImportError} />}
        </Panel>
      </div>

      {result && (
        <Panel
          title="本次导入结果"
          extra={<Tag color={statusMeta[result.status].color}>{statusMeta[result.status].label}</Tag>}
        >
          <Alert
            className="mb-4"
            type={result.status === 'failed' ? 'error' : result.status === 'partial' ? 'warning' : 'success'}
            showIcon
            message={result.message}
            description={
              <span>
                文件范围：<DateRange start={result.file_start_date} end={result.file_end_date} />
                {result.overlap_start_date && (
                  <>；与旧数据重叠：<DateRange start={result.overlap_start_date} end={result.overlap_end_date} /></>
                )}
                ；导入后数据库：<DateRange start={result.coverage.start_date} end={result.coverage.end_date} />
              </span>
            }
          />
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-7">
            {(result.import_type === 'rating'
              ? [
                ['新增评定', result.inserted_rows],
                ['更新评定', result.updated_rows],
                ['重复未变', result.unchanged_rows],
                ['无法匹配', result.unmatched_rows || 0],
                ['匹配有歧义', result.ambiguous_rows || 0],
                ['未采用', result.ignored_rows],
                ['错误/提醒', result.error_count + result.warning_count],
              ]
              : [
                ['新增', result.inserted_rows],
                ['更新', result.updated_rows],
                ['重复未变', result.unchanged_rows],
                ['忽略', result.ignored_rows],
                ['错误', result.error_count],
                ['提醒', result.warning_count],
              ]).map(([label, value]) => (
              <div key={String(label)} className="rounded-lg border border-slate-200 p-4">
                <Statistic title={label} value={value} suffix="条" />
              </div>
            ))}
          </div>
        </Panel>
      )}

      {result && issueTotal > 0 && (
        <Panel
          title={`错误和提醒（${issueTotal} 条）`}
          description={result.import_type === 'rating'
            ? '无法匹配或存在歧义的星级评定不会强行写入走访记录'
            : '身份证号已经遮盖；错误行未入库，提醒行不影响有效数据入库'}
          padded={false}
        >
          <AppTable<VisitImportIssue>
            columns={result.import_type === 'rating' ? ratingIssueColumns : detailIssueColumns}
            dataSource={issues}
            rowKey="id"
            loading={issueLoading}
            pagination={{
              current: issuePage,
              pageSize: ISSUE_PAGE_SIZE,
              total: issueTotal,
              showSizeChanger: false,
              showTotal: total => `共 ${total} 条`,
              onChange: loadIssuePage,
            }}
            scroll={{ x: 1360 }}
          />
        </Panel>
      )}

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
