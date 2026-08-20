import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Button, DatePicker, Empty, Input, Select, Spin, Table, Tabs, Tag, message } from 'antd'
import dayjs, { type Dayjs } from 'dayjs'
import { ReloadOutlined } from '@ant-design/icons'
import { useSearchParams } from 'react-router-dom'
import { PageHeader, Panel } from '../components/ui'
import ExternalDataPanel from '../components/ExternalDataPanel'
import { useAuth } from '../context/AuthContext'
import {
  fetchCodeSummaries,
  formatDateInTimezone,
  formatUTCTime,
  getCodeSummary,
  getExternalAcquisitionRun,
  getLatestExternalAcquisitionRun,
  recomputeCodeSummaryLocations,
  recordXlsxExport,
  saveCodeSummaryLocationClassifications,
  searchCodeSummaryLocations,
  type CodeSummaryReport,
  type CodeSummaryRow,
  type CodeSummarySource,
  type CodeSummaryLocationReport,
} from '../api/client'
import { exportSummaryWorkbook } from '../utils/summaryXlsx'

const { RangePicker } = DatePicker
const MAX_CODE_SUMMARY_DAYS = 31

function rangeDays(start: string, end: string) {
  return dayjs(end).diff(dayjs(start), 'day') + 1
}

function apiErrorMessage(reason: any, fallback: string) {
  const detail = reason?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const messages = detail
      .map(item => typeof item?.msg === 'string' ? item.msg : '')
      .filter(Boolean)
    if (messages.length) return messages.join('；')
  }
  return fallback
}

const SOURCE_OPTIONS = [
  { label: '平安码', value: 'peace' as const },
  { label: '管家码', value: 'manager' as const },
]

function percent(value: number | undefined) {
  return `${((value || 0) * 100).toFixed(1)}%`
}

function columns(source: CodeSummarySource) {
  const base = [
    { title: '业务日期', dataIndex: 'business_date', key: 'business_date', fixed: 'left' as const },
    { title: source === 'peace' ? '总人数' : '当日扫码人数', dataIndex: 'total_people', key: 'total_people' },
  ]
  if (source === 'peace') {
    return [
      ...base,
      { title: '巡防扫码数', dataIndex: 'patrol_scan_count', key: 'patrol_scan_count' },
      { title: '接处警大厅扫码数', dataIndex: 'dispatch_hall_scan_count', key: 'dispatch_hall_scan_count' },
      { title: '户籍大厅扫码数', dataIndex: 'household_hall_scan_count', key: 'household_hall_scan_count' },
      { title: '社会面扫码数', dataIndex: 'social_scan_count', key: 'social_scan_count' },
      { title: '未分类扫码数', dataIndex: 'unclassified_scan_count', key: 'unclassified_scan_count' },
      { title: '产生指令数', dataIndex: 'instruction_count', key: 'instruction_count' },
      { title: '有效预警率', dataIndex: 'effective_warning_rate', key: 'effective_warning_rate', render: percent },
      { title: '新增登记数', dataIndex: 'new_registration_count', key: 'new_registration_count' },
      { title: '有效扫码率', dataIndex: 'effective_scan_rate', key: 'effective_scan_rate', render: percent },
    ]
  }
  return [
    ...base,
    { title: '活跃账户', dataIndex: 'active_accounts', key: 'active_accounts' },
    { title: '产生指令数', dataIndex: 'instruction_count', key: 'instruction_count' },
    { title: '有效预警率', dataIndex: 'effective_warning_rate', key: 'effective_warning_rate', render: percent },
  ]
}

function summaryRows(report: CodeSummaryReport | null, includeTotal: boolean): CodeSummaryRow[] {
  if (!report) return []
  if (!includeTotal) return report.data
  return [...report.data, { ...report.total, business_date: '总计' }]
}

const EXPORT_COLUMNS = {
  peace: [
    ['业务日期', 'business_date'], ['总人数', 'total_people'],
    ['巡防扫码数', 'patrol_scan_count'], ['接处警大厅扫码数', 'dispatch_hall_scan_count'],
    ['户籍大厅扫码数', 'household_hall_scan_count'], ['社会面扫码数', 'social_scan_count'],
    ['未分类扫码数', 'unclassified_scan_count'], ['产生指令数', 'instruction_count'],
    ['有效预警率', 'effective_warning_rate'], ['新增登记数', 'new_registration_count'],
    ['有效扫码率', 'effective_scan_rate'],
  ],
  manager: [
    ['业务日期', 'business_date'], ['当日扫码人数', 'total_people'],
    ['活跃账户', 'active_accounts'], ['产生指令数', 'instruction_count'],
    ['有效预警率', 'effective_warning_rate'],
  ],
} as const

function exportTable(report: CodeSummaryReport, source: CodeSummarySource) {
  const mapping = EXPORT_COLUMNS[source]
  const mapRow = (row: CodeSummaryRow) => Object.fromEntries(
    mapping.map(([label, key]) => [label, row[key as keyof CodeSummaryRow] ?? 0]),
  )
  return {
    sheet: source === 'peace' ? '平安码' : '管家码',
    columns: mapping.map(([label]) => label),
    rows: report.data.map(mapRow),
    total: mapRow(report.total),
  }
}

export default function CodeSummary() {
  const { user, systemTimezone } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const today = formatDateInTimezone(new Date(), systemTimezone)
  const initialStart = searchParams.get('start') || today
  const initialEnd = searchParams.get('end') || today
  const [range, setRange] = useState<[Dayjs, Dayjs]>([dayjs(initialStart), dayjs(initialEnd)])
  const [source, setSource] = useState<CodeSummarySource>(searchParams.get('source') === 'manager' ? 'manager' : 'peace')
  const [report, setReport] = useState<CodeSummaryReport | null>(null)
  const [loading, setLoading] = useState(false)
  const [fetching, setFetching] = useState(false)
  const [fetchJob, setFetchJob] = useState<import('../api/client').ExternalAcquisitionRun | null>(null)
  const [error, setError] = useState('')
  const [locationReport, setLocationReport] = useState<CodeSummaryLocationReport | null>(null)
  const [locationLoading, setLocationLoading] = useState(false)
  const [locationKeyword, setLocationKeyword] = useState('')
  const [locationStatus, setLocationStatus] = useState<'all' | 'unclassified' | 'classified'>('unclassified')
  const [locationPage, setLocationPage] = useState(1)
  const [locationSaving, setLocationSaving] = useState(false)
  const canFetch = Boolean(user?.permissions.includes('visit.source.manage'))
  const canManageLocations = Boolean(user?.permissions.includes('code.summary.manage'))

  const startDate = range[0].format('YYYY-MM-DD')
  const endDate = range[1].format('YYYY-MM-DD')

  const load = useCallback(async () => {
    if (rangeDays(startDate, endDate) > MAX_CODE_SUMMARY_DAYS) {
      setReport(null)
      setError(`单次最多查询 ${MAX_CODE_SUMMARY_DAYS} 天，请拆分日期范围后再读取`)
      return
    }
    setLoading(true)
    setError('')
    try {
      setReport(await getCodeSummary(source, startDate, endDate))
    } catch (reason: any) {
      setError(apiErrorMessage(reason, '码数据汇总读取失败'))
    } finally {
      setLoading(false)
    }
  }, [endDate, source, startDate])

  useEffect(() => { void load() }, [load])
  const loadLocations = useCallback(async () => {
    if (source !== 'peace') return
    setLocationLoading(true)
    try {
      setLocationReport(await searchCodeSummaryLocations({
        source, start_date: startDate, end_date: endDate,
        keyword: locationKeyword, status: locationStatus, page: locationPage, page_size: 20,
      }))
    } catch (reason: any) {
      setError(apiErrorMessage(reason, '位置分类数据读取失败'))
    } finally {
      setLocationLoading(false)
    }
  }, [endDate, locationKeyword, locationPage, locationStatus, source, startDate])
  useEffect(() => { void loadLocations() }, [loadLocations])
  useEffect(() => {
    let active = true
    let timer: number | undefined
    const resume = async () => {
      try {
        const latest = await getLatestExternalAcquisitionRun('code_summary_fetch')
        if (!active || !latest) return
        setFetchJob(latest)
        if (latest.status === 'queued' || latest.status === 'running') {
          const poll = async () => {
            try {
              const current = await getExternalAcquisitionRun(latest.id)
              if (!active) return
              setFetchJob(current)
              if (current.status === 'queued' || current.status === 'running') {
                timer = window.setTimeout(() => void poll(), 1500)
              } else {
                await load()
              }
            } catch {
              // 页面恢复时的进度查询失败不覆盖已有快照。
            }
          }
          timer = window.setTimeout(() => void poll(), 0)
        }
      } catch {
        // 无法恢复进度时仍可正常查看已保存快照。
      }
    }
    void resume()
    return () => {
      active = false
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [load])
  useEffect(() => {
    const next = new URLSearchParams()
    next.set('start', startDate)
    next.set('end', endDate)
    next.set('source', source)
    setSearchParams(next, { replace: true })
  }, [endDate, setSearchParams, source, startDate])

  const handleFetch = async () => {
    if (rangeDays(startDate, endDate) > MAX_CODE_SUMMARY_DAYS) {
      setError(`单次最多获取 ${MAX_CODE_SUMMARY_DAYS} 天，请拆分日期范围后再获取`)
      return
    }
    setFetching(true)
    setError('')
    try {
      const result = await fetchCodeSummaries(startDate, endDate)
      setFetchJob(result.run)
      const poll = async (): Promise<void> => {
        const current = await getExternalAcquisitionRun(result.run.id)
        setFetchJob(current)
        if (current.status === 'queued' || current.status === 'running') {
          window.setTimeout(() => void poll(), 1500)
          return
        }
        const items = current.result?.results || []
        const failed = items.filter((item: any) => item.status === 'failed')
        const warning = items.filter((item: any) => item.status === 'warning')
        if (failed.length) message.warning('部分来源获取失败，旧快照已保留')
        else if (warning.length) message.info('数据已更新，但存在质量提醒')
        else if (current.status === 'success') message.success('平安码、管家码数据已更新')
        await load()
        setFetching(false)
      }
      void poll()
    } catch (reason: any) {
      setFetching(false)
      setError(apiErrorMessage(reason, '码数据获取失败，旧快照未改变'))
    }
  }

  const handleExport = async () => {
    if (rangeDays(startDate, endDate) > MAX_CODE_SUMMARY_DAYS) {
      message.warning(`单次最多导出 ${MAX_CODE_SUMMARY_DAYS} 天，请拆分日期范围后再导出`)
      return
    }
    try {
      const [peace, manager] = await Promise.all([
        getCodeSummary('peace', startDate, endDate),
        getCodeSummary('manager', startDate, endDate),
      ])
      await Promise.all(([
        ['平安码', peace], ['管家码', manager],
      ] as const).map(([summaryType, item]) => recordXlsxExport({
          export_type: 'code_summary',
          start_date: startDate,
          end_date: endDate,
          summary_type: summaryType,
          inspector_rows: item.data.length,
          community_rows: 0,
        })))
      await exportSummaryWorkbook({
        fileName: `平安码管家码汇总_${startDate}_至_${endDate}`,
        tables: [exportTable(peace, 'peace'), exportTable(manager, 'manager')],
      })
      message.success('已导出当前码数据汇总')
    } catch (reason: any) {
      message.error(apiErrorMessage(reason, '导出失败，请稍后重试'))
    }
  }

  const tableColumns = useMemo(() => columns(source), [source])
  const rows = summaryRows(report, startDate !== endDate)
  const locationColumns = [
    { title: '采集位置', dataIndex: 'display_name', key: 'display_name', ellipsis: true },
    { title: '出现次数', dataIndex: 'record_count', key: 'record_count', width: 100 },
    { title: '最近日期', dataIndex: 'last_seen_date', key: 'last_seen_date', width: 120 },
    { title: '当前分类', dataIndex: 'classification', key: 'classification', width: 170,
      render: (value: string, row: any) => canManageLocations ? <Select
        size="small" value={value === 'unclassified' ? undefined : value} placeholder="选择分类" style={{ width: 145 }}
        options={[['social', '社会面'], ['patrol', '巡防'], ['dispatch_hall', '接警大厅'], ['household_hall', '户政大厅'], ['ignored', '忽略/无效位置'], ['other', '其他']].map(([v, label]) => ({ value: v, label }))}
        onChange={async next => {
          setLocationSaving(true)
          try {
            await saveCodeSummaryLocationClassifications({ source: 'peace', items: [{ location_key: row.location_key, display_name: row.display_name, classification: next }] })
            await recomputeCodeSummaryLocations(startDate, endDate)
            await Promise.all([load(), loadLocations()])
            message.success('位置分类已保存')
          } catch (reason: any) { message.error(apiErrorMessage(reason, '位置分类保存失败')) }
          finally { setLocationSaving(false) }
        }} /> : (value === 'unclassified' ? '未分类' : ({ social: '社会面', patrol: '巡防', dispatch_hall: '接警大厅', household_hall: '户政大厅', ignored: '忽略/无效位置', other: '其他' } as any)[value] || value) },
  ]

  return (
    <div className="app-page min-w-0">
      <PageHeader title="平安码/管家码汇总" description="按日期查看平安码和管家码去重后的扫码、指令和登记指标" />
      <ExternalDataPanel
        title="平安码与管家码数据获取"
        description="来源数据只在服务器端读取；获取失败不会覆盖已保存的每日快照。"
        actions={canFetch ? <><Button type="primary" icon={<ReloadOutlined />} loading={fetching} onClick={() => void handleFetch()}>自动获取平安码管家码数据</Button><Button onClick={() => void handleExport()}>导出 XLSX</Button></> : <Button onClick={() => void handleExport()}>导出 XLSX</Button>}
        controls={<label><span>业务日期范围</span><RangePicker
          value={range}
          allowClear={false}
          onChange={value => {
            if (value?.[0] && value[1]) setRange([value[0], value[1]])
          }}
        /></label>}
        stats={[
          { label: '系统时区', value: systemTimezone },
          {
            label: '最近获取',
            value: <Tag color={!report?.latest_run ? 'default' : report.latest_run.status === 'failed' ? 'error' : report.latest_run.status === 'warning' ? 'warning' : 'success'}>
              {report?.latest_run
                ? report.latest_run.status === 'success' ? '成功' : report.latest_run.status === 'warning' ? '有质量提醒' : '失败'
                : '尚未获取'}
            </Tag>,
          },
          { label: '最近成功', value: report?.latest_success_at ? formatUTCTime(report.latest_success_at, systemTimezone) : '暂无' },
          { label: '当前区间', value: `${startDate} 至 ${endDate}` },
        ]}
        progress={fetchJob && (fetchJob.status === 'queued' || fetchJob.status === 'running') ? {
          label: '后台获取任务',
          status: fetchJob.status === 'queued' ? '等待执行' : '执行中',
          detail: `${fetchJob.message || fetchJob.phase}${fetchJob.total ? ` · ${fetchJob.current}/${fetchJob.total}` : ''}`,
          percent: fetchJob.progress ?? 0,
        } : undefined}
      >
        {report?.latest_run?.error_message && <Alert type="warning" showIcon message={report.latest_run.error_message} />}
        {report?.latest_run?.status === 'warning' && report.latest_run.invalid_time_count > 0 && (
          <Alert
            type="warning"
            showIcon
            message={`${report.latest_run.invalid_time_count} 条来源记录缺少有效 comparisonTime，已跳过；其余正常数据已更新`}
          />
        )}
        {report?.latest_run?.status === 'warning' && (
          <Alert
            type="info"
            showIcon
            message={`质量提醒：${[
              report.latest_run.excluded_count ? `${report.latest_run.excluded_count} 条身份信息无法识别` : '',
              report.latest_run.unclassified_count ? `${report.latest_run.unclassified_count} 条位置无法分类` : '',
              report.latest_run.invalid_time_count ? `${report.latest_run.invalid_time_count} 条时间无效已跳过` : '',
            ].filter(Boolean).join('；') || '来源数据存在非阻断质量问题，已保留可用数据'}`}
          />
        )}
        {error && <Alert type="error" showIcon message={error} />}
        {source === 'peace' && (
          <Panel title="位置分类核查" className="mt-3" extra={canManageLocations ? <Button size="small" loading={locationSaving} onClick={async () => { try { await recomputeCodeSummaryLocations(startDate, endDate); await Promise.all([load(), loadLocations()]); message.success('已重新计算当前汇总') } catch (reason: any) { message.error(apiErrorMessage(reason, '重新计算失败')) } }}>重新计算汇总</Button> : undefined}>
            {Boolean(report?.latest_run?.unclassified_count) && !locationLoading && locationReport?.total === 0 && (
              <Alert className="mb-3" type="info" showIcon message="旧快照没有保存逐位置明细，请重新获取当前日期范围后再进行分类核查。" />
            )}
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <span>当前区间未分类记录：{locationReport?.unclassified_count ?? report?.latest_run?.unclassified_count ?? 0} 条</span>
              <Input.Search allowClear placeholder="搜索位置" style={{ width: 240 }} value={locationKeyword} onChange={event => { setLocationKeyword(event.target.value); setLocationPage(1) }} onSearch={() => { setLocationPage(1); void loadLocations() }} />
              <Select value={locationStatus} onChange={value => { setLocationStatus(value); setLocationPage(1) }} options={[{ value: 'unclassified', label: '仅未分类' }, { value: 'classified', label: '已分类' }, { value: 'all', label: '全部位置' }]} />
            </div>
            <Spin spinning={locationLoading}>
              <Table size="small" rowKey="location_key" columns={locationColumns as any} dataSource={locationReport?.data || []} pagination={{ current: locationPage, pageSize: 20, total: locationReport?.total || 0, showSizeChanger: false, onChange: page => setLocationPage(page) }} scroll={{ x: 650 }} />
            </Spin>
            {!canManageLocations && <div className="mt-2 text-xs text-slate-500">当前账号只有查看权限；管理员可在此维护位置分类。</div>}
          </Panel>
        )}
      </ExternalDataPanel>
      <Panel title={source === 'peace' ? '平安码宽表' : '管家码宽表'}>
        <Tabs activeKey={source} items={SOURCE_OPTIONS.map(item => ({ key: item.value, label: item.label }))} onChange={value => setSource(value as CodeSummarySource)} />
        {source === 'peace' && <Alert className="mb-3" type="info" showIcon message="新增登记数按全链条创建时间和核查结果实时统计；当天数据可能随核查进度变化。" />}
        <Spin spinning={loading}>
          {rows.length ? <Table
            rowKey={row => `${source}-${row.business_date}-${row.version || 'total'}`}
            columns={tableColumns as any}
            dataSource={rows}
            pagination={false}
            scroll={{ x: source === 'peace' ? 1300 : 760 }}
            rowClassName={row => row.business_date === '总计' ? 'font-semibold' : ''}
          /> : <Empty description="当前区间没有已保存快照" />}
        </Spin>
      </Panel>
    </div>
  )
}
