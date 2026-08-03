import { useState, useEffect, useCallback } from 'react'
import { Alert, DatePicker, Segmented, Select, Table, Tag } from 'antd'
import type { TableColumnsType } from 'antd'
import dayjs from 'dayjs'
import AppTable from '../components/AppTable'
import DataOverview from '../components/DataOverview'
import MobileReportTable from '../components/MobileReportTable'
import SummaryReportConfigButton from '../components/SummaryReportConfigButton'
import SyncPanel from '../components/SyncPanel'
import { EmptyState, PageHeader, Panel } from '../components/ui'
import {
  formatDateInTimezone,
  getOnlineDataOverview,
  getReport,
  getReportRange,
  getReportTypes,
  getSystemConfig,
  type OnlineDataOverview,
} from '../api/client'
import { useAuth } from '../context/AuthContext'
import { useSync } from '../hooks/useSync'
import { buildReportTableTotal } from '../utils/tableTotals'

const RATE_COLS = ['核查完成率', '核查见底率']
const EMPTY_FILTER_VALUE = '__binhu_empty_report_value__'

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
  const { user, recordActivity } = useAuth()
  const reportColumnMode = user?.report_column_mode || 'three'
  const today = formatDateInTimezone()
  const [dateRange, setDateRange] = useState<[string, string]>([today, today])
  const [reportType, setReportType] = useState('全链条')
  const [types, setTypes] = useState<string[]>([])
  const [implemented, setImplemented] = useState<string[]>([])
  const [report, setReport] = useState<any>({ exists: false })
  const [overview, setOverview] = useState<OnlineDataOverview | null>(null)
  const [overviewLoading, setOverviewLoading] = useState(false)
  const [overviewError, setOverviewError] = useState('')
  const [msg, setMsg] = useState('')
  const [timezone, setTimezone] = useState('Asia/Shanghai')
  const [mobileReportSection, setMobileReportSection] = useState<'inspector' | 'community'>('inspector')
  const [startDate, endDate] = dateRange

  // 日期或业务类型变化时读取报表；同步任务结束时也会调用同一函数。
  const fetchReport = useCallback(async () => {
    if (startDate > endDate) return
    setOverviewLoading(true)
    setOverviewError('')
    try {
      // 同一天走单日查询（查日报表，工作量口径）；不同天走区间查询（查快照存量）
      const reportRequest = startDate === endDate
        ? getReport(startDate, reportType, reportColumnMode)
        : getReportRange(startDate, endDate, reportType, reportColumnMode)
      const [reportResult, overviewResult] = await Promise.allSettled([
        reportRequest,
        getOnlineDataOverview(startDate, endDate, reportType),
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
  }, [startDate, endDate, reportType, reportColumnMode])

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
    getSystemConfig().then(c => {
      const configuredTimezone = c.timezone || 'Asia/Shanghai'
      setTimezone(configuredTimezone)
      setDateRange(current => {
        if (current[0] !== today || current[1] !== today) return current
        const configuredToday = formatDateInTimezone(new Date(), configuredTimezone)
        return [configuredToday, configuredToday]
      })
    }).catch(() => {})
  }, [])
  useEffect(() => { getReportTypes().then((r) => { setTypes(r.data); setImplemented(r.implemented) }).catch(() => {}) }, [])
  useEffect(() => { fetchReport() }, [fetchReport])

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
        timezone={timezone}
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
              { key: 'carryover', title: '结转数据', value: overview?.carryover_tasks || 0, suffix: '条', help: '进入所选区间时已经存在、尚未完成的任务', valueStyle: { color: '#d97706' } },
              { key: 'new', title: '新下发数据', value: overview?.new_tasks || 0, suffix: '条', help: '任务首次进入区间时，前一张快照中还不存在', valueStyle: { color: '#1d4ed8' } },
              { key: 'changed', title: '已有任务变化', value: overview?.changed_tasks || 0, suffix: '条', help: '前一张快照中已经存在，但发生了有效业务变化' },
              { key: 'pending', title: '待完成', value: overview?.pending_tasks || 0, suffix: '条', valueStyle: { color: '#dc2626' } },
              { key: 'completed', title: '已完成', value: overview?.completed_tasks || 0, suffix: '条', help: `完成率 ${((overview?.completion_rate || 0) * 100).toFixed(1)}%`, valueStyle: { color: '#047857' } },
            ]}
          />
        </Panel>
      )}

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
