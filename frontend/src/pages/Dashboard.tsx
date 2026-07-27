import { useState, useEffect, useCallback, useRef } from 'react'
import { Button, DatePicker, Select, Tag } from 'antd'
import dayjs from 'dayjs'
import SyncPanel from '../components/SyncPanel'
import { EmptyState, PageHeader } from '../components/ui'
import { buildReport, formatDateInTimezone, getReport, getReportRange, getReportTypes, triggerSync, getSyncStatus, getSystemConfig } from '../api/client'
import { getDisplayMode } from '../utils/displayMode'

const RATE_COLS = ['核查完成率', '核查见底率']
const fmt = (val: any, col: string) => {
  if (val == null) return '-'
  if (RATE_COLS.includes(col)) return `${(val * 100).toFixed(0)}%`
  return String(val)
}

export default function Dashboard() {
  const today = formatDateInTimezone()
  const [dateRange, setDateRange] = useState<[string, string]>([today, today])
  const [reportType, setReportType] = useState('全链条')
  const [types, setTypes] = useState<string[]>([])
  const [implemented, setImplemented] = useState<string[]>([])
  const [report, setReport] = useState<any>({ exists: false })
  const [building, setBuilding] = useState(false)
  const [msg, setMsg] = useState('')
  const [timezone, setTimezone] = useState('Asia/Shanghai')
  // 同步状态
  const [syncing, setSyncing] = useState(false)
  const [syncStatus, setSyncStatus] = useState<any>(null)
  const [syncError, setSyncError] = useState<string | null>(null)
  const pollRef = useRef<number | null>(null)

  const handleSync = async () => {
    setSyncing(true); setSyncError(null)
    try {
      await triggerSync()
      const poll = async () => {
        const st = await getSyncStatus()
        setSyncStatus(st)
        if (st.status === 'running' || st.status === 'pending') {
          pollRef.current = window.setTimeout(poll, 2000)
        } else {
          setSyncing(false)
          if (st.status === 'failed') setSyncError(st.error_message || '同步失败')
        }
      }
      poll()
    } catch (e: any) {
      setSyncing(false); setSyncError(e?.response?.data?.detail || '触发同步失败')
    }
  }

  useEffect(() => { getSyncStatus().then(setSyncStatus).catch(() => {}) }, [])
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
  useEffect(() => () => { if (pollRef.current) clearTimeout(pollRef.current) }, [])

  useEffect(() => { getReportTypes().then((r) => { setTypes(r.data); setImplemented(r.implemented) }).catch(() => {}) }, [])

  // 状态驱动：dateRange 或 reportType 变化 → 自动 fetch
  const [startDate, endDate] = dateRange
  const fetchReport = useCallback(async () => {
    if (startDate > endDate) return
    try {
      // 同一天走单日查询（查日报表，工作量口径）；不同天走区间查询（查快照存量）
      const res = startDate === endDate
        ? await getReport(startDate, reportType)
        : await getReportRange(startDate, endDate, reportType)
      setReport(res)
      setMsg(!res.exists ? (res.message || `${startDate} 暂无「${reportType}」日报`) : '')
    } catch (e: any) {
      const status = e?.response?.status
      const detail = e?.response?.data?.detail || e?.message
      setMsg(`查询失败(${status || '?'})：${detail || '网络错误'}`)
      setReport({ exists: false })
    }
  }, [startDate, endDate, reportType])

  useEffect(() => { fetchReport() }, [fetchReport])

  const handleBuild = async () => {
    setBuilding(true); setMsg('')
    try {
      const res = await buildReport({ date: startDate, parser_type: reportType })
      if (res.implemented === false) { setMsg(res.message); return }
      if (reportType === '总汇总表') {
        setMsg(`生成成功：${startDate} · ${res.rows} 个社区`)
      } else {
        setMsg(`生成成功：${startDate} · 核查人 ${res.inspector_rows} 行，社区 ${res.community_rows} 行`)
      }
      fetchReport()
    } catch (e: any) { setMsg('生成失败') }
    finally { setBuilding(false) }
  }

  const isImplemented = implemented.includes(reportType)
  const isSummary = reportType === '总汇总表'
  const isRange = startDate !== endDate
  const rangeInfo = report.range
  const cardMode = getDisplayMode() === 'card'

  // 卡片渲染辅助
  const renderCard = (row: Record<string, any>, columns: string[], titleCols: string[]) => (
    <div className="app-card app-card--padded space-y-1.5">
      <div className="flex items-center justify-between border-b pb-2 mb-1">
        <span className="font-semibold text-gray-800">{titleCols.map(c => row[c]).filter(Boolean).join(' · ')}</span>
      </div>
      <div className="grid grid-cols-3 gap-2 text-sm">
        {columns.filter(c => !titleCols.includes(c) && c !== 'id').map(col => (
          <div key={col}>
            <span className="text-gray-400 text-xs block">{col}</span>
            <span className="text-gray-800">{fmt(row[col], col)}</span>
          </div>
        ))}
      </div>
    </div>
  )

  return (
    <div className="app-page">
      <PageHeader
        title="在线数据汇总"
        description="同步腾讯文档数据，并按日期和业务类型查看统计结果"
        actions={report.exists ? (
          <Tag color="blue">
            {isSummary
              ? `${report.data?.length || 0} 个社区`
              : `核查人 ${report.inspector?.data.length || 0} 行 · 社区 ${report.community?.data.length || 0} 行`}
          </Tag>
        ) : undefined}
      />

      <SyncPanel syncing={syncing} status={syncStatus} error={syncError} onSync={handleSync} timezone={timezone} />

      <section className="app-card">
        <div className="app-toolbar dashboard-report-toolbar">
          <Select
            size="large"
            value={reportType}
            onChange={setReportType}
            className="w-full md:w-40"
            options={types.map(type => ({
              value: type,
              label: `${type}${!implemented.includes(type) ? '（待对接）' : ''}`,
            }))}
          />
          {/* 移动端：原生 date input */}
          <div className="md:hidden flex items-center gap-1.5 w-full">
            <input type="date" value={startDate} onChange={(e) => setDateRange([e.target.value, e.target.value > endDate ? e.target.value : endDate])}
              className="border border-gray-300 rounded px-2 py-1.5 text-sm flex-1" />
            <span className="text-gray-400 text-xs">至</span>
            <input type="date" value={endDate} onChange={(e) => setDateRange([e.target.value > startDate ? e.target.value : startDate, e.target.value])}
              className="border border-gray-300 rounded px-2 py-1.5 text-sm flex-1" />
          </div>
          {/* 桌面端：Ant Design RangePicker */}
          <div className="hidden w-[272px] md:block">
            <DatePicker.RangePicker
              size="large"
              className="w-full"
              value={[dayjs(startDate), dayjs(endDate)]}
              onChange={(_, dateStrings) => {
                if (dateStrings[0] && dateStrings[1]) setDateRange([dateStrings[0], dateStrings[1]])
              }}
              allowClear={false}
            />
          </div>
          <Button
            type="primary"
            size="large"
            className="dashboard-report-toolbar__build"
            onClick={handleBuild}
            loading={building}
            disabled={!isImplemented}
          >
            生成日报
          </Button>
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
        {isRange && (
          <p className="border-t border-slate-100 px-5 py-2.5 text-xs text-slate-500">
            区间模式下，“生成日报”只会生成起始日期（{startDate}）的日报。
          </p>
        )}
      </section>

      {!isImplemented ? (
        <section className="app-card">
          <EmptyState label={`“${reportType}”的统计规则尚未对接`} />
        </section>
      ) : !report.exists ? (
        <section className="app-card">
          <EmptyState label={msg || `${startDate} 至 ${endDate} 暂无“${reportType}”报告`} />
        </section>
      ) : isSummary ? (
        cardMode ? (
          <div className="space-y-3">
            <h3 className="text-sm font-semibold text-gray-700 px-1">
              总汇总表（{report.data?.length || 0} 个社区）
              {isRange && <span className="text-gray-400 font-normal ml-2">{rangeInfo?.start} 至 {rangeInfo?.end} 聚合</span>}
            </h3>
            <div className="grid grid-cols-1 gap-3">
              {report.data?.map((row: any, i: number) => renderCard(row, report.columns || [], ['社区']))}
            </div>
          </div>
        ) : (
        <div className="app-table-wrap">
          <div className="px-4 py-2 border-b bg-gray-50">
            <h3 className="text-sm font-semibold text-gray-700">
              总汇总表（{report.data?.length || 0} 个社区）
              {isRange && <span className="text-gray-400 font-normal ml-2">{rangeInfo?.start} 至 {rangeInfo?.end} 聚合</span>}
            </h3>
          </div>
          <table className="app-table min-w-full">
            <thead className="bg-gray-50 border-b sticky top-0 z-10"><tr>
              {report.columns?.filter((c: string) => c !== 'id').map((col: string) => (
                <th key={col} className="px-3 py-2 text-left font-medium text-gray-600 whitespace-nowrap">{col}</th>
              ))}
            </tr></thead>
            <tbody className="divide-y divide-gray-100">
              {report.data?.map((row: any, i: number) => (
                <tr key={i} className="hover:bg-gray-50 font-medium">
                  {report.columns?.filter((c: string) => c !== 'id').map((col: string) => (
                    <td key={col} className="px-3 py-2 text-gray-800 whitespace-nowrap">{fmt(row[col], col)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        )
      ) : cardMode ? (
        <div className="space-y-6">
          <div className="space-y-3">
            <h3 className="text-sm font-semibold text-gray-700 px-1">
              核查人明细统计（{report.inspector?.data.length || 0} 人）
              {isRange && <span className="text-gray-400 font-normal ml-2">{rangeInfo?.start} 至 {rangeInfo?.end} 聚合</span>}
            </h3>
            <div className="grid grid-cols-1 gap-3">
              {report.inspector?.data.map((row, i) => renderCard(row, report.inspector?.columns || [], ['社区', '姓名']))}
            </div>
          </div>
          <div className="space-y-3">
            <h3 className="text-sm font-semibold text-gray-700 px-1">
              社区汇总统计（{report.community?.data.length || 0} 个社区）
              {isRange && <span className="text-gray-400 font-normal ml-2">{rangeInfo?.start} 至 {rangeInfo?.end} 聚合</span>}
            </h3>
            <div className="grid grid-cols-1 gap-3">
              {report.community?.data.map((row, i) => renderCard(row, report.community?.columns || [], ['社区']))}
            </div>
          </div>
        </div>
      ) : (
        <>
          <div className="app-table-wrap">
            <div className="px-4 py-2 border-b bg-gray-50">
              <h3 className="text-sm font-semibold text-gray-700">
                核查人明细统计（{report.inspector?.data.length || 0} 人）
                {isRange && <span className="text-gray-400 font-normal ml-2">{rangeInfo?.start} 至 {rangeInfo?.end} 聚合</span>}
              </h3>
            </div>
            <table className="app-table min-w-full">
              <thead className="bg-gray-50 border-b sticky top-0 z-10"><tr>
                {report.inspector?.columns.filter(c => c !== 'id').map((col) => (
                  <th key={col} className="px-3 py-2 text-left font-medium text-gray-600 whitespace-nowrap">{col}</th>
                ))}
              </tr></thead>
              <tbody className="divide-y divide-gray-100">
                {report.inspector?.data.map((row, i) => (
                  <tr key={i} className="hover:bg-gray-50">
                    {report.inspector?.columns.filter(c => c !== 'id').map((col) => (
                      <td key={col} className="px-3 py-2 text-gray-800 whitespace-nowrap">{fmt(row[col], col)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="app-table-wrap">
            <div className="px-4 py-2 border-b bg-gray-50">
              <h3 className="text-sm font-semibold text-gray-700">
                社区汇总统计（{report.community?.data.length || 0} 个社区）
                {isRange && <span className="text-gray-400 font-normal ml-2">{rangeInfo?.start} 至 {rangeInfo?.end} 聚合</span>}
              </h3>
            </div>
            <table className="app-table min-w-full">
              <thead className="bg-gray-50 border-b sticky top-0 z-10"><tr>
                {report.community?.columns.filter(c => c !== 'id').map((col) => (
                  <th key={col} className="px-3 py-2 text-left font-medium text-gray-600 whitespace-nowrap">{col}</th>
                ))}
              </tr></thead>
              <tbody className="divide-y divide-gray-100">
                {report.community?.data.map((row, i) => (
                  <tr key={i} className="hover:bg-gray-50 font-medium">
                    {report.community?.columns.filter(c => c !== 'id').map((col) => (
                      <td key={col} className="px-3 py-2 text-gray-800 whitespace-nowrap">{fmt(row[col], col)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

    </div>
  )
}
