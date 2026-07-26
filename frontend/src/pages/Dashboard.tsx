import { useState, useEffect, useCallback, useRef } from 'react'
import { DatePicker } from 'antd'
import dayjs from 'dayjs'
import SyncPanel from '../components/SyncPanel'
import { buildReport, getReport, getReportRange, listReports, getReportTypes, triggerSync, getSyncStatus, getSystemConfig } from '../api/client'
import { getDisplayMode } from '../utils/displayMode'

const RATE_COLS = ['核查完成率', '核查见底率']
const fmt = (val: any, col: string) => {
  if (val == null) return '-'
  if (RATE_COLS.includes(col)) return `${(val * 100).toFixed(0)}%`
  return String(val)
}

export default function Dashboard() {
  const today = new Date().toISOString().slice(0, 10)
  const [dateRange, setDateRange] = useState<[string, string]>([today, today])
  const [reportType, setReportType] = useState('全链条')
  const [types, setTypes] = useState<string[]>([])
  const [implemented, setImplemented] = useState<string[]>([])
  const [report, setReport] = useState<any>({ exists: false })
  const [reports, setReports] = useState<{ date: string; type: string }[]>([])
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
  useEffect(() => { getSystemConfig().then(c => setTimezone(c.timezone || 'Asia/Shanghai')).catch(() => {}) }, [])
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
  const fetchReports = useCallback(async () => { try { setReports(await listReports()) } catch {} }, [])

  useEffect(() => { fetchReport() }, [fetchReport])
  useEffect(() => { fetchReports() }, [fetchReports])

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
      fetchReport(); fetchReports()
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
    <div className="bg-white rounded-lg shadow p-4 space-y-1.5">
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
    <div className="space-y-4">
      <SyncPanel syncing={syncing} status={syncStatus} error={syncError} onSync={handleSync} timezone={timezone} />

      <div className="bg-white rounded-lg shadow p-4">
        <div className="flex flex-wrap gap-3 items-center">
          <select value={reportType} onChange={(e) => setReportType(e.target.value)} className="border border-gray-300 rounded px-3 py-1.5 text-sm">
            {types.map((t) => <option key={t} value={t}>{t}{!implemented.includes(t) ? '（待对接）' : ''}</option>)}
          </select>
          {/* 移动端：原生 date input */}
          <div className="md:hidden flex items-center gap-1.5 w-full">
            <input type="date" value={startDate} onChange={(e) => setDateRange([e.target.value, e.target.value > endDate ? e.target.value : endDate])}
              className="border border-gray-300 rounded px-2 py-1.5 text-sm flex-1" />
            <span className="text-gray-400 text-xs">至</span>
            <input type="date" value={endDate} onChange={(e) => setDateRange([e.target.value > startDate ? e.target.value : startDate, e.target.value])}
              className="border border-gray-300 rounded px-2 py-1.5 text-sm flex-1" />
          </div>
          {/* 桌面端：Ant Design RangePicker */}
          <div className="hidden md:block">
            <DatePicker.RangePicker
              value={[dayjs(startDate), dayjs(endDate)]}
              onChange={(_, dateStrings) => {
                if (dateStrings[0] && dateStrings[1]) setDateRange([dateStrings[0], dateStrings[1]])
              }}
              allowClear={false}
            />
          </div>
          <button onClick={handleBuild} disabled={building || !isImplemented} className="px-4 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50">
            {building ? '生成中...' : '生成日报'}
          </button>
          {msg && <span className={`text-sm ${msg.includes('成功') ? 'text-green-600' : 'text-orange-500'}`}>{msg}</span>}
          {report.exists && (
            <span className="text-sm text-gray-500 ml-auto">
              {isRange && rangeInfo ? `${rangeInfo.start} 至 ${rangeInfo.end}（${rangeInfo.days} 天）· ` : ''}
              {isSummary ? `${report.data?.length || 0} 个社区` : `核查人 ${report.inspector?.data.length || 0} 行 · 社区 ${report.community?.data.length || 0} 行`}
            </span>
          )}
        </div>
        {isRange && <p className="text-xs text-gray-400 mt-1.5">区间模式下"生成日报"将针对起始日期（{startDate}）生成</p>}
      </div>

      {!isImplemented ? (
        <div className="bg-white rounded-lg shadow p-8 text-center text-gray-400 text-sm">
          「{reportType}」的统计规则尚未对接，请后续逐个定义
        </div>
      ) : !report.exists ? (
        <div className="bg-white rounded-lg shadow p-8 text-center text-gray-400 text-sm">
          {msg || `${startDate} 至 ${endDate} 暂无「${reportType}」报告`}
        </div>
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
        <div className="bg-white rounded-lg shadow overflow-auto">
          <div className="px-4 py-2 border-b bg-gray-50">
            <h3 className="text-sm font-semibold text-gray-700">
              总汇总表（{report.data?.length || 0} 个社区）
              {isRange && <span className="text-gray-400 font-normal ml-2">{rangeInfo?.start} 至 {rangeInfo?.end} 聚合</span>}
            </h3>
          </div>
          <table className="min-w-full text-sm">
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
          <div className="bg-white rounded-lg shadow overflow-auto">
            <div className="px-4 py-2 border-b bg-gray-50">
              <h3 className="text-sm font-semibold text-gray-700">
                核查人明细统计（{report.inspector?.data.length || 0} 人）
                {isRange && <span className="text-gray-400 font-normal ml-2">{rangeInfo?.start} 至 {rangeInfo?.end} 聚合</span>}
              </h3>
            </div>
            <table className="min-w-full text-sm">
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

          <div className="bg-white rounded-lg shadow overflow-auto">
            <div className="px-4 py-2 border-b bg-gray-50">
              <h3 className="text-sm font-semibold text-gray-700">
                社区汇总统计（{report.community?.data.length || 0} 个社区）
                {isRange && <span className="text-gray-400 font-normal ml-2">{rangeInfo?.start} 至 {rangeInfo?.end} 聚合</span>}
              </h3>
            </div>
            <table className="min-w-full text-sm">
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

      {reports.length > 0 && (
        <div className="bg-white rounded-lg shadow p-4">
          <h3 className="text-sm font-semibold text-gray-700 mb-3">历史日报</h3>
          <div className="flex flex-wrap gap-2">
            {reports.map((r) => (
              <button key={r.date + r.type}
                onClick={() => { setDateRange([r.date, r.date]); setReportType(r.type) }}
                className={`px-3 py-1.5 rounded text-xs border ${r.date === startDate && r.date === endDate && r.type === reportType ? 'bg-blue-600 text-white border-blue-600' : 'border-gray-300 text-gray-600 hover:bg-gray-50'}`}>
                {r.date} {r.type}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
