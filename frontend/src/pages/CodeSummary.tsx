import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Button, DatePicker, Empty, Spin, Table, Tabs, Tag, message } from 'antd'
import dayjs, { type Dayjs } from 'dayjs'
import { ReloadOutlined } from '@ant-design/icons'
import { useSearchParams } from 'react-router-dom'
import { PageHeader, Panel } from '../components/ui'
import { useAuth } from '../context/AuthContext'
import {
  fetchCodeSummaries,
  formatDateInTimezone,
  formatUTCTime,
  getCodeSummary,
  recordXlsxExport,
  type CodeSummaryReport,
  type CodeSummaryRow,
  type CodeSummarySource,
} from '../api/client'
import { exportSummaryWorkbook } from '../utils/summaryXlsx'

const { RangePicker } = DatePicker

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
      { title: '估算新增登记数', dataIndex: 'new_registration_count', key: 'new_registration_count' },
      { title: '估算有效扫码率', dataIndex: 'effective_scan_rate', key: 'effective_scan_rate', render: percent },
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
    ['有效预警率', 'effective_warning_rate'], ['估算新增登记数', 'new_registration_count'],
    ['估算有效扫码率', 'effective_scan_rate'],
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
  const [error, setError] = useState('')
  const canFetch = Boolean(user?.permissions.includes('visit.source.manage'))

  const startDate = range[0].format('YYYY-MM-DD')
  const endDate = range[1].format('YYYY-MM-DD')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setReport(await getCodeSummary(source, startDate, endDate))
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '码数据汇总读取失败')
    } finally {
      setLoading(false)
    }
  }, [endDate, source, startDate])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    const next = new URLSearchParams()
    next.set('start', startDate)
    next.set('end', endDate)
    next.set('source', source)
    setSearchParams(next, { replace: true })
  }, [endDate, setSearchParams, source, startDate])

  const handleFetch = async () => {
    setFetching(true)
    setError('')
    try {
      const result = await fetchCodeSummaries(startDate, endDate)
      const failed = result.data.filter(item => item.status === 'failed')
      const warning = result.data.filter(item => item.status === 'warning')
      if (failed.length) {
        const failedNames = failed.map(item => item.source === 'peace' ? '平安码' : '管家码').join('、')
        const succeededNames = result.data
          .filter(item => item.status !== 'failed')
          .map(item => item.source === 'peace' ? '平安码' : '管家码')
          .join('、')
        message.warning(`${succeededNames ? `${succeededNames}已更新；` : ''}${failedNames}获取失败，旧快照已保留`)
      }
      else if (warning.length) message.info('数据已更新，但存在未分类或无效身份证质量提醒')
      else message.success('平安码、管家码数据已更新')
      await load()
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '码数据获取失败，旧快照未改变')
    } finally {
      setFetching(false)
    }
  }

  const handleExport = async () => {
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
      message.error(reason?.response?.data?.detail || '导出失败，请稍后重试')
    }
  }

  const tableColumns = useMemo(() => columns(source), [source])
  const rows = summaryRows(report, startDate !== endDate)

  return (
    <div className="app-page min-w-0">
      <PageHeader title="平安码/管家码汇总" description="按日期查看平安码和管家码去重后的扫码、指令和登记指标" />
      <Panel
        title="数据获取"
        description="来源数据只在服务器端读取；获取失败不会覆盖已保存的每日快照。"
        extra={canFetch ? <div className="flex flex-wrap gap-2"><Button icon={<ReloadOutlined />} loading={fetching} onClick={() => void handleFetch()}>自动获取平安码管家码数据</Button><Button onClick={() => void handleExport()}>导出 XLSX</Button></div> : <Button onClick={() => void handleExport()}>导出 XLSX</Button>}
      >
        <div className="flex flex-wrap items-center gap-3">
          <RangePicker
            value={range}
            allowClear={false}
            onChange={value => {
              if (value?.[0] && value[1]) setRange([value[0], value[1]])
            }}
          />
          <span className="text-sm text-[var(--app-text-secondary)]">系统时区：{systemTimezone}</span>
          <Tag color={report?.latest_run?.status === 'failed' ? 'error' : report?.latest_run?.status === 'warning' ? 'warning' : 'default'}>
            {report?.latest_run
              ? `最近获取：${report.latest_run.status === 'success' ? '成功' : report.latest_run.status === 'warning' ? '有质量提醒' : '失败'}`
              : '尚未获取'}
          </Tag>
          <span className="text-sm text-[var(--app-text-secondary)]">
            最近成功：{report?.latest_success_at ? formatUTCTime(report.latest_success_at, systemTimezone) : '暂无'}
          </span>
        </div>
        {report?.latest_run?.error_message && <Alert className="mt-3" type="warning" showIcon message={report.latest_run.error_message} />}
        {report?.latest_run?.status === 'warning' && report.latest_run.invalid_time_count > 0 && (
          <Alert
            className="mt-3"
            type="warning"
            showIcon
            message={`${report.latest_run.invalid_time_count} 条来源记录缺少有效 comparisonTime，已跳过；其余正常数据已更新`}
          />
        )}
        {error && <Alert className="mt-3" type="error" showIcon message={error} />}
      </Panel>
      <Panel title={source === 'peace' ? '平安码宽表' : '管家码宽表'}>
        <Tabs activeKey={source} items={SOURCE_OPTIONS.map(item => ({ key: item.value, label: item.label }))} onChange={value => setSource(value as CodeSummarySource)} />
        {source === 'peace' && <Alert className="mb-3" type="info" showIcon message="估算新增登记数按每日产生指令数的 8%–12% 稳定计算；同一天相同指令数不会因刷新发生变化。" />}
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
