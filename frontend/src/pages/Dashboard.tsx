import { useState, useEffect, useCallback } from 'react'
import {
  Alert,
  Button,
  DatePicker,
  Drawer,
  Empty,
  message,
  Pagination,
  Segmented,
  Select,
  Skeleton,
  Table,
  Tag,
} from 'antd'
import type { TableColumnsType } from 'antd'
import { DownloadOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'
import { useSearchParams } from 'react-router-dom'
import AppTable from '../components/AppTable'
import DataOverview from '../components/DataOverview'
import MobileReportTable from '../components/MobileReportTable'
import SummaryReportConfigButton from '../components/SummaryReportConfigButton'
import SyncPanel from '../components/SyncPanel'
import { EmptyState, PageHeader, Panel } from '../components/ui'
import {
  formatDateInTimezone,
  getOnlineDataOverview,
  getOnlineDataOverviewDetails,
  getReport,
  getReportRange,
  getReportTypes,
  recordXlsxExport,
  type OnlineDataOverview,
  type OnlineOverviewCategory,
  type OnlineOverviewDetailItem,
} from '../api/client'
import { useAuth } from '../context/AuthContext'
import { useSync } from '../hooks/useSync'
import { exportSummaryWorkbook } from '../utils/summaryXlsx'
import { buildReportTableTotal } from '../utils/tableTotals'

const RATE_COLS = ['核查完成率', '核查见底率']
const EMPTY_FILTER_VALUE = '__binhu_empty_report_value__'
const OVERVIEW_STATE_LABELS: Record<string, { text: string; color: string }> = {
  unchecked: { text: '未核查', color: 'red' },
  checked: { text: '待补结果', color: 'orange' },
  completed: { text: '已完成', color: 'green' },
}

const fmt = (val: any, col: string) => {
  if (val == null) return '-'
  if (RATE_COLS.includes(col)) return `${(val * 100).toFixed(0)}%`
  return String(val)
}

const compareReportValues = (left: unknown, right: unknown, sortOrder?: string | null) => {
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

const reportTableColumns = (
  columns: string[],
  rows: Record<string, any>[],
): TableColumnsType<Record<string, any>> =>
  columns
    .filter(column => column !== 'id')
    .map(column => {
      const filterOptions = new Map<string, { text: string; value: string; raw: unknown }>()
      for (const row of rows) {
        const raw = row[column]
        const value = raw == null || raw === '' ? EMPTY_FILTER_VALUE : String(raw)
        if (!filterOptions.has(value)) {
          filterOptions.set(value, {
            text: fmt(raw, column),
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
        sorter: (left, right, sortOrder) => compareReportValues(left[column], right[column], sortOrder),
        filters: Array.from(filterOptions.values())
          .sort((left, right) => compareReportValues(left.raw, right.raw))
          .map(({ text, value }) => ({ text, value })),
        filterSearch: true,
        onFilter: (selectedValue, row) => {
          const raw = row[column]
          const rowValue = raw == null || raw === '' ? EMPTY_FILTER_VALUE : String(raw)
          return rowValue === String(selectedValue)
        },
        render: (value: unknown) => fmt(value, column),
      }
    })

const reportTableSummary = (
  columns: string[],
) => (currentRows: readonly Record<string, any>[]) => {
  const visibleColumns = columns.filter(column => column !== 'id')
  const summary = buildReportTableTotal(visibleColumns, currentRows)
  return (
    <Table.Summary.Row className="app-report-total-row">
      {visibleColumns.map((column, index) => (
        <Table.Summary.Cell index={index} key={column}>
          <span className={index === 0 ? 'font-semibold text-blue-900' : ''}>
            {fmt(summary[column], column)}
          </span>
        </Table.Summary.Cell>
      ))}
    </Table.Summary.Row>
  )
}

export default function Dashboard() {
  const { user, recordActivity, systemTimezone } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const reportColumnMode = user?.report_column_mode || 'three'
  const browserToday = formatDateInTimezone()
  const requestedStart = searchParams.get('start') || browserToday
  const requestedEnd = searchParams.get('end') || requestedStart
  const [dateRange, setDateRange] = useState<[string, string]>([requestedStart, requestedEnd])
  const [reportType, setReportType] = useState(searchParams.get('type') || '全链条')
  const responsibilityScope = searchParams.get('scope') === 'responsibility' ? 'responsibility' : 'permission'
  const requestedCommunity = searchParams.get('community') || ''
  const [types, setTypes] = useState<string[]>([])
  const [implemented, setImplemented] = useState<string[]>([])
  const [report, setReport] = useState<any>({ exists: false })
  const [overview, setOverview] = useState<OnlineDataOverview | null>(null)
  const [overviewLoading, setOverviewLoading] = useState(false)
  const [overviewError, setOverviewError] = useState('')
  const [msg, setMsg] = useState('')
  const [mobileReportSection, setMobileReportSection] = useState<'inspector' | 'community'>('inspector')
  const [visibleInspectorRows, setVisibleInspectorRows] = useState<Record<string, any>[]>([])
  const [visibleCommunityRows, setVisibleCommunityRows] = useState<Record<string, any>[]>([])
  const [exporting, setExporting] = useState(false)
  const requestedCategory = searchParams.get('category')
  const [detailCategory, setDetailCategory] = useState<OnlineOverviewCategory | null>(
    ['carryover', 'new', 'changed', 'pending', 'completed'].includes(requestedCategory || '')
      ? requestedCategory as OnlineOverviewCategory
      : null,
  )
  const [detailRows, setDetailRows] = useState<OnlineOverviewDetailItem[]>([])
  const [detailTotal, setDetailTotal] = useState(0)
  const [detailPage, setDetailPage] = useState(1)
  const [detailLabel, setDetailLabel] = useState('')
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')
  const [startDate, endDate] = dateRange

  // 日期或业务类型变化时读取报表；同步任务结束时也会调用同一函数。
  const fetchReport = useCallback(async () => {
    if (startDate > endDate) return
    setOverviewLoading(true)
    setOverviewError('')
    try {
      // 同一天走单日查询（查日报表，工作量口径）；不同天走区间查询（查快照存量）
      const reportRequest = startDate === endDate
        ? getReport(startDate, reportType, reportColumnMode, { scope: responsibilityScope, community: requestedCommunity || undefined })
        : getReportRange(startDate, endDate, reportType, reportColumnMode, { scope: responsibilityScope, community: requestedCommunity || undefined })
      const [reportResult, overviewResult] = await Promise.allSettled([
        reportRequest,
        getOnlineDataOverview(startDate, endDate, reportType, { scope: responsibilityScope, community: requestedCommunity || undefined }),
      ])
      if (reportResult.status === 'rejected') {
        throw reportResult.reason
      }
      const res = reportResult.value
      setReport(res)
      setMsg(!res.exists ? (res.message || `${startDate} 暂无「${reportType}」日报`) : '')
      if (overviewResult.status === 'fulfilled') {
        setOverview(overviewResult.value)
      } else {
        setOverview(null)
        setOverviewError('数据概览读取失败，汇总表仍可正常查看')
      }
    } catch (e: any) {
      const status = e?.response?.status
      const detail = e?.response?.data?.detail || e?.message
      setMsg(`查询失败(${status || '?'})：${detail || '网络错误'}`)
      setReport({ exists: false })
      setOverview(null)
      setOverviewError('数据概览读取失败，请稍后重试')
    } finally {
      setOverviewLoading(false)
    }
  }, [startDate, endDate, reportType, reportColumnMode, requestedCommunity, responsibilityScope])

  const {
    syncing,
    status: syncStatus,
    taskError: syncTaskError,
    statusError: syncStatusError,
    actionError: syncActionError,
    startSync: handleSync,
  } = useSync(fetchReport)
  const canManualSync = Boolean(user?.permissions.includes('sync.trigger'))
  const canConfigureReport = Boolean(user?.permissions.includes('report.config.manage'))

  useEffect(() => {
    const configuredToday = formatDateInTimezone(new Date(), systemTimezone)
    setDateRange(current => (
      current[0] === browserToday && current[1] === browserToday
        ? [configuredToday, configuredToday]
        : current
    ))
  }, [browserToday, systemTimezone])
  useEffect(() => { getReportTypes().then((r) => { setTypes(r.data); setImplemented(r.implemented) }).catch(() => {}) }, [])
  useEffect(() => { fetchReport() }, [fetchReport])
  useEffect(() => {
    setDetailRows([])
    setDetailTotal(0)
    setDetailPage(1)
  }, [startDate, endDate, reportType])

  useEffect(() => {
    const next = new URLSearchParams()
    next.set('start', startDate)
    next.set('end', endDate)
    next.set('type', reportType)
    if (responsibilityScope === 'responsibility') next.set('scope', 'responsibility')
    if (requestedCommunity) next.set('community', requestedCommunity)
    if (detailCategory) next.set('category', detailCategory)
    setSearchParams(next, { replace: true })
  }, [detailCategory, endDate, reportType, requestedCommunity, responsibilityScope, setSearchParams, startDate])

  const isImplemented = implemented.includes(reportType)
  const isSummary = reportType === '总汇总表'
  const isRange = startDate !== endDate
  const rangeInfo = report.range
  const inspectorTable = report.inspector || { columns: [], data: [] }
  const communityTable = report.community || (
    isSummary
      ? {
          columns: report.columns || [],
          data: report.data || [],
          summary: report.summary,
        }
      : { columns: [], data: [] }
  )
  const mobileTable = mobileReportSection === 'inspector'
    ? inspectorTable
    : communityTable
  const mobileTitleColumns = mobileReportSection === 'inspector'
    ? ['社区', '姓名']
    : ['社区']
  const inspectorTitle = isSummary ? '总汇总 · 网格员明细' : '核查人明细统计'
  const communityTitle = isSummary ? '总汇总 · 社区汇总' : '社区汇总统计'
  const availableRange = overview?.available_start_date
    && overview?.available_end_date
    ? `${overview.available_start_date} 至 ${overview.available_end_date}`
    : '暂无可用数据'

  useEffect(() => {
    setVisibleInspectorRows(inspectorTable.data)
    setVisibleCommunityRows(communityTable.data)
  }, [report])

  const handleExport = async () => {
    setExporting(true)
    try {
      await recordActivity()
      const inspectorColumns = inspectorTable.columns.filter((column: string) => column !== 'id')
      const communityColumns = communityTable.columns.filter((column: string) => column !== 'id')
      await recordXlsxExport({
        export_type: 'online_summary',
        start_date: startDate,
        end_date: endDate,
        summary_type: reportType,
        inspector_rows: visibleInspectorRows.length,
        community_rows: visibleCommunityRows.length,
      })
      await exportSummaryWorkbook({
        fileName: `在线数据汇总_${reportType}_${startDate}_至_${endDate}`,
        tables: [
          {
            sheet: '网格员汇总',
            columns: inspectorColumns,
            rows: visibleInspectorRows,
            total: buildReportTableTotal(inspectorColumns, visibleInspectorRows),
          },
          {
            sheet: '社区汇总',
            columns: communityColumns,
            rows: visibleCommunityRows,
            total: buildReportTableTotal(communityColumns, visibleCommunityRows),
          },
        ],
      })
      message.success('已导出当前在线汇总数据')
    } catch (error) {
      message.error(error instanceof Error ? error.message : '导出失败，请稍后重试')
    } finally {
      setExporting(false)
    }
  }

  const loadOverviewDetails = async (
    category: OnlineOverviewCategory,
    page = 1,
  ) => {
    setDetailCategory(category)
    setDetailLoading(true)
    setDetailError('')
    try {
      await recordActivity()
      const result = await getOnlineDataOverviewDetails({
        start_date: startDate,
        end_date: endDate,
        parser_type: reportType,
        category,
        page,
        page_size: 20,
        scope: responsibilityScope,
        community: requestedCommunity || undefined,
      })
      setDetailRows(result.data)
      setDetailTotal(result.total)
      setDetailPage(result.page)
      setDetailLabel(result.category_label)
    } catch (error: any) {
      setDetailRows([])
      setDetailTotal(0)
      setDetailError(error?.response?.data?.detail || error?.message || '概览明细读取失败')
    } finally {
      setDetailLoading(false)
    }
  }

  useEffect(() => {
    if (detailCategory) {
      void loadOverviewDetails(detailCategory, 1)
    }
  }, [startDate, endDate, reportType])

  const overviewDetailColumns: TableColumnsType<OnlineOverviewDetailItem> = [
    {
      title: '业务',
      dataIndex: 'parser_type',
      width: 130,
    },
    {
      title: '核查对象',
      key: 'subject',
      width: 190,
      render: (_, item) => (
        <div>
          <div className="font-medium text-[var(--app-text-strong)]">{item.summary.title}</div>
          {item.summary.identity_number && (
            <div className="mt-0.5 text-xs text-[var(--app-text-secondary)]">{item.summary.identity_number}</div>
          )}
        </div>
      ),
    },
    {
      title: '社区 / 核查人',
      key: 'assignment',
      width: 150,
      render: (_, item) => (
        <div>
          <div>{item.community || '未填写社区'}</div>
          <div className="mt-0.5 text-xs text-[var(--app-text-secondary)]">{item.inspector || '待分配'}</div>
        </div>
      ),
    },
    {
      title: '状态',
      key: 'state',
      width: 180,
      render: (_, item) => {
        const state = OVERVIEW_STATE_LABELS[item.state] || { text: item.state || '未知', color: 'default' }
        return (
          <div>
            <Tag color={state.color}>{state.text}</Tag>
            <div className="mt-1 text-xs leading-5 text-[var(--app-text-secondary)]">{item.reason}</div>
          </div>
        )
      },
    },
    {
      title: '日期信息',
      key: 'activity',
      width: 170,
      render: (_, item) => (
        <div className="text-xs leading-5">
          <div><span className="text-[var(--app-text-secondary)]">最近活动：</span>{item.last_activity_date}</div>
          <div><span className="text-[var(--app-text-secondary)]">首次下发：</span>{item.first_dispatch_date || item.first_activity_date}</div>
          {item.first_activity_date !== item.last_activity_date && (
            <div><span className="text-[var(--app-text-secondary)]">区间首次：</span>{item.first_activity_date}</div>
          )}
        </div>
      ),
    },
    {
      title: '电话 / 地址',
      key: 'contact',
      width: 260,
      render: (_, item) => (
        <div className="text-sm leading-5">
          {item.summary.phone && <div>{item.summary.phone}</div>}
          <div className="text-[var(--app-text-secondary)]">{item.summary.address || '未填写地址'}</div>
          {item.summary.result && <div className="mt-1 text-xs text-[var(--app-primary)]">结果：{item.summary.result}</div>}
        </div>
      ),
    },
  ]

  return (
    <div className="app-page">
      <PageHeader
        title="在线数据汇总"
        description="同步腾讯文档数据，并按日期和业务类型查看统计结果"
        actions={report.exists ? (
          <Tag color="blue">
            网格员 {inspectorTable.data.length} 行 · 社区 {communityTable.data.length} 行
          </Tag>
        ) : undefined}
      />

      <SyncPanel
        syncing={syncing}
        status={syncStatus}
        taskError={syncTaskError}
        statusError={syncStatusError}
        actionError={syncActionError}
        onSync={handleSync}
        canManualSync={canManualSync}
        timezone={systemTimezone}
      />

      {isImplemented && (
        <Panel
          title="在线数据概览"
          description={`${reportType} · ${startDate} 至 ${endDate}，概览与当前查询条件保持一致`}
        >
          {overviewError && (
            <Alert
              className="mb-3"
              type="warning"
              showIcon
              message={overviewError}
            />
          )}
          <DataOverview
            loading={overviewLoading}
            rangeTitle="可用日报日期范围"
            rangeValue={availableRange}
            rangeDescription={overview?.available_data_days
              ? `共 ${overview.available_data_days} 个可用日期；当前选中 ${overview.selected_data_days} 天`
              : '完成一次成功同步后，这里会显示可用范围'}
            metrics={[
              { key: 'total', title: '任务总数', value: overview?.total_tasks || 0, suffix: '条', help: '所选区间内同一业务任务去重后的数量' },
              { key: 'carryover', title: '结转数据', value: overview?.carryover_tasks || 0, suffix: '条', help: '进入所选区间时已经存在、尚未完成的任务；点击查看明细', valueStyle: { color: '#d97706' }, onClick: () => void loadOverviewDetails('carryover') },
              { key: 'new', title: '新下发数据', value: overview?.new_tasks || 0, suffix: '条', help: '任务首次进入区间时，前一张快照中还不存在；点击查看明细', valueStyle: { color: '#1d4ed8' }, onClick: () => void loadOverviewDetails('new') },
              { key: 'changed', title: '已有任务变化', value: overview?.changed_tasks || 0, suffix: '条', help: '前一张快照中已经存在，但发生了有效业务变化；点击查看明细', onClick: () => void loadOverviewDetails('changed') },
              { key: 'pending', title: '待完成', value: overview?.pending_tasks || 0, suffix: '条', help: '区间最终状态尚未完成；点击查看明细', valueStyle: { color: '#dc2626' }, onClick: () => void loadOverviewDetails('pending') },
              { key: 'completed', title: '已完成', value: overview?.completed_tasks || 0, suffix: '条', help: `完成率 ${((overview?.completion_rate || 0) * 100).toFixed(1)}%；点击查看明细`, valueStyle: { color: '#047857' }, onClick: () => void loadOverviewDetails('completed') },
            ]}
          />
        </Panel>
      )}

      <Drawer
        open={Boolean(detailCategory)}
        onClose={() => setDetailCategory(null)}
        width="min(920px, 100vw)"
        title={`${detailLabel || '概览'}明细 · ${startDate} 至 ${endDate}`}
        extra={!detailLoading ? <Tag color="blue">共 {detailTotal} 条</Tag> : undefined}
        destroyOnHidden
      >
        {detailError ? (
          <Alert
            type="error"
            showIcon
            message={detailError}
            action={detailCategory ? (
              <Button size="small" onClick={() => void loadOverviewDetails(detailCategory, detailPage)}>重试</Button>
            ) : undefined}
          />
        ) : detailLoading ? (
          <Skeleton active paragraph={{ rows: 8 }} />
        ) : detailRows.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前分类没有数据" />
        ) : (
          <>
            <div className="hidden md:block">
              <Table
                rowKey={item => `${item.parser_type}:${item.row_key}`}
                columns={overviewDetailColumns}
                dataSource={detailRows}
                pagination={false}
                size="small"
                scroll={{ x: 1030 }}
              />
            </div>
            <div className="space-y-3 md:hidden">
              {detailRows.map(item => {
                const state = OVERVIEW_STATE_LABELS[item.state] || { text: item.state || '未知', color: 'default' }
                return (
                  <article key={`${item.parser_type}:${item.row_key}`} className="app-card p-4">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="text-xs text-[var(--app-text-secondary)]">{item.parser_type}</div>
                        <h3 className="mt-1 font-semibold text-[var(--app-text-strong)]">{item.summary.title}</h3>
                        <div className="mt-1 text-xs text-[var(--app-text-secondary)]">{item.community || '未填写社区'} · {item.inspector || '待分配'}</div>
                      </div>
                      <Tag color={state.color}>{state.text}</Tag>
                    </div>
                    <div className="mt-3 space-y-1.5 text-sm text-[var(--app-text)]">
                      {item.summary.identity_number && <div>身份证号：{item.summary.identity_number}</div>}
                      {item.summary.phone && <div>电话：{item.summary.phone}</div>}
                      <div>地址：{item.summary.address || '未填写'}</div>
                      {item.summary.result && <div>结果：{item.summary.result}</div>}
                    </div>
                    <div className="mt-3 border-t border-[var(--app-border)] pt-3 text-xs leading-5 text-[var(--app-text-secondary)]">
                      <div>{item.reason}</div>
                      <div>最近活动 {item.last_activity_date}</div>
                      <div>首次下发 {item.first_dispatch_date || item.first_activity_date}</div>
                      {item.first_activity_date !== item.last_activity_date && (
                        <div>区间首次 {item.first_activity_date}</div>
                      )}
                    </div>
                  </article>
                )
              })}
            </div>
            {detailTotal > 20 && (
              <div className="mt-5 flex justify-center">
                <Pagination
                  current={detailPage}
                  pageSize={20}
                  total={detailTotal}
                  showSizeChanger={false}
                  onChange={page => {
                    if (detailCategory) void loadOverviewDetails(detailCategory, page)
                  }}
                />
              </div>
            )}
          </>
        )}
      </Drawer>

      <section className="app-card">
        <div className="app-toolbar dashboard-report-toolbar">
          <Select
            size="large"
            value={reportType}
            onChange={(value) => {
              void recordActivity().catch(() => {})
              setReportType(value)
            }}
            className="w-full md:w-40"
            options={types.map(type => ({
              value: type,
              label: `${type}${!implemented.includes(type) ? '（待对接）' : ''}`,
            }))}
          />
          {/* 移动端：原生 date input */}
          <div className="md:hidden flex items-center gap-1.5 w-full">
            <input type="date" value={startDate} onChange={(e) => {
              void recordActivity().catch(() => {})
              setDateRange([e.target.value, e.target.value > endDate ? e.target.value : endDate])
            }}
              className="border border-gray-300 rounded px-2 py-1.5 text-sm flex-1" />
            <span className="text-gray-400 text-xs">至</span>
            <input type="date" value={endDate} onChange={(e) => {
              void recordActivity().catch(() => {})
              setDateRange([e.target.value > startDate ? e.target.value : startDate, e.target.value])
            }}
              className="border border-gray-300 rounded px-2 py-1.5 text-sm flex-1" />
          </div>
          {/* 桌面端：Ant Design RangePicker */}
          <div className="hidden w-[272px] md:block">
            <DatePicker.RangePicker
              size="large"
              className="w-full"
              value={[dayjs(startDate), dayjs(endDate)]}
              onChange={(_, dateStrings) => {
                if (dateStrings[0] && dateStrings[1]) {
                  void recordActivity().catch(() => {})
                  setDateRange([dateStrings[0], dateStrings[1]])
                }
              }}
              allowClear={false}
            />
          </div>
          {canConfigureReport && <SummaryReportConfigButton />}
          {report.exists && (
            <Button
              size="large"
              icon={<DownloadOutlined />}
              loading={exporting}
              onClick={handleExport}
              className="w-full md:w-auto"
            >
              导出 XLSX
            </Button>
          )}
          {msg && report.exists && (
            <span className={`text-sm ${msg.includes('成功') ? 'text-green-700' : 'text-orange-700'}`}>
              {msg}
            </span>
          )}
          {isRange && rangeInfo && (
            <span className="ml-auto text-sm text-slate-500">
              {rangeInfo.start} 至 {rangeInfo.end}（{rangeInfo.days} 天）
            </span>
          )}
        </div>
        <div className="border-t border-slate-100 px-5 py-2.5 text-xs leading-5 text-slate-500">
          {isRange ? (
            <p>区间内同一任务只计算一次，并按区间结束时的状态归类。</p>
          ) : (
            <p>单日数据总数包含前期未完成任务和当天新增、变更的任务；日报随同步自动生成。</p>
          )}
        </div>
      </section>

      {report.scope_message && (
        <Alert type="info" showIcon message={report.scope_message} />
      )}
      {report.attendance && !report.attendance.complete && (
        <Alert
          type="warning"
          showIcon
          message="所选区间含未完成的双休日排班，已隐藏每日人均核查数"
          description={report.attendance.missing_week_starts?.length
            ? `缺少排班的周：${report.attendance.missing_week_starts.join('、')}`
            : undefined}
        />
      )}

      {!isImplemented ? (
        <section className="app-card">
          <EmptyState label={`“${reportType}”的统计规则尚未对接`} />
        </section>
      ) : !report.exists ? (
        <section className="app-card">
          <EmptyState label={msg || `${startDate} 至 ${endDate} 暂无“${reportType}”报告`} />
        </section>
      ) : (
        <>
          <div className="space-y-3 md:hidden">
            <Segmented
              block
              value={mobileReportSection}
              onChange={value => setMobileReportSection(value as 'inspector' | 'community')}
              options={[
                {
                  label: `网格员 ${inspectorTable.data.length}`,
                  value: 'inspector',
                },
                {
                  label: `社区 ${communityTable.data.length}`,
                  value: 'community',
                },
              ]}
            />
            <MobileReportTable
              columns={mobileTable?.columns || []}
              rows={mobileTable?.data || []}
              titleColumns={mobileTitleColumns}
              resetKey={`${mobileReportSection}-${reportType}-${startDate}-${endDate}-${reportColumnMode}`}
              fullColumns={reportTableColumns(
                mobileTable?.columns || [],
                mobileTable?.data || [],
              )}
              fullSummary={reportTableSummary(mobileTable?.columns || [])}
              rowKey={(row, index) => (
                row.id
                || (mobileReportSection === 'inspector'
                  ? `${row.社区}-${row.姓名}-${index}`
                  : row.社区)
                || index
              )}
            />
          </div>

          <div className="hidden space-y-6 md:block">
            <AppTable<Record<string, any>>
              key={`inspector-${reportType}-${startDate}-${endDate}-${reportColumnMode}`}
              columns={reportTableColumns(inspectorTable.columns, inspectorTable.data)}
              dataSource={inspectorTable.data}
              reportGrid
              rowKey={row => row.id || `${row.社区}-${row.姓名}`}
              summary={inspectorTable.summary
                ? reportTableSummary(
                    inspectorTable.columns,
                  )
                : undefined}
              onChange={(_, __, ___, extra) => {
                setVisibleInspectorRows([...extra.currentDataSource])
              }}
              title={currentRows => (
                <h3 className="text-sm font-semibold text-gray-700">
                  {inspectorTitle}（{currentRows.length} 人）
                  {isRange && <span className="text-gray-400 font-normal ml-2">{rangeInfo?.start} 至 {rangeInfo?.end} 聚合</span>}
                </h3>
              )}
            />

            <AppTable<Record<string, any>>
              key={`community-${reportType}-${startDate}-${endDate}-${reportColumnMode}`}
              columns={reportTableColumns(communityTable.columns, communityTable.data)}
              dataSource={communityTable.data}
              reportGrid
              rowKey={row => row.id || row.社区}
              summary={communityTable.summary
                ? reportTableSummary(
                    communityTable.columns,
                  )
                : undefined}
              onChange={(_, __, ___, extra) => {
                setVisibleCommunityRows([...extra.currentDataSource])
              }}
              title={currentRows => (
                <h3 className="text-sm font-semibold text-gray-700">
                  {communityTitle}（{currentRows.length} 个社区）
                  {isRange && <span className="text-gray-400 font-normal ml-2">{rangeInfo?.start} 至 {rangeInfo?.end} 聚合</span>}
                </h3>
              )}
            />
          </div>
        </>
      )}

    </div>
  )
}
