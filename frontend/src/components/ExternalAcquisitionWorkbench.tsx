import { useEffect, useMemo, useState } from 'react'
import { Alert, Button, DatePicker, Empty, Input, Progress, Select, Switch, Tag, message } from 'antd'
import dayjs, { type Dayjs } from 'dayjs'
import { useSearchParams } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import {
  ExternalAcquisitionCatalogItem,
  ExternalAcquisitionRun,
  getExternalAcquisitionCatalog,
  getExternalAcquisitionRuns,
  retryExternalAcquisitionRun,
  runExternalAcquisitionSource,
  updateExternalAcquisitionSchedule,
  formatUTCTime,
} from '../api/client'
import {
  EXTERNAL_RUN_POLL_INTERVAL_MS,
  hasActiveExternalAcquisitionRun,
} from '../utils/externalAcquisitionPolling'

const SOURCE_ALIASES: Record<string, string> = { registry: 'registry_certificate', txdocs: 'txdocs_monitor', qmf: 'qmf_source', residence: 'residence_full_scan', visits: 'visit_source', codes: 'code_summary', venue: 'venue_cloud' }
const { RangePicker } = DatePicker
const MAX_MANUAL_RANGE_DAYS = 31
const COMMON_CRONS = [
  { label: '每天 02:00', value: '0 2 * * *' }, { label: '每天 06:00', value: '0 6 * * *' },
  { label: '每天 12:00', value: '0 12 * * *' }, { label: '每小时', value: '0 * * * *' },
  { label: '每 30 分钟', value: '*/30 * * * *' },
]

function isSuperAdmin(user: any) {
  return user?.role === 'super_admin' || user?.permission_groups?.some((group: any) => group.code === 'super_admin') || user?.permission_group?.code === 'super_admin'
}
function runLabel(run: ExternalAcquisitionRun | null | undefined) {
  if (!run) return '尚未运行'
  if (run.status === 'success') return '成功'
  if (run.status === 'warning') return '有提醒'
  if (run.status === 'failed') return '失败'
  if (run.status === 'interrupted') return '已中断'
  if (run.status === 'running') return '运行中'
  return '排队中'
}

const CODE_SUMMARY_SOURCE_LABELS: Record<string, string> = {
  peace: '平安码',
  manager: '管家码',
}

function CodeSummaryQuality({ run }: { run: ExternalAcquisitionRun }) {
  const quality = run.result?.quality
  if (!quality) return null
  const sourceCounts = Object.entries(quality) as [string, any][]
  const unclassifiedCount = sourceCounts.reduce((total, [, counts]) => total + Number(counts.unclassified_location_count || 0), 0)
  const outOfRangeCount = sourceCounts.reduce((total, [, counts]) => total + Number(counts.out_of_range_count || 0), 0)
  const successfulWithWarnings = run.status === 'success' && (unclassifiedCount > 0 || outOfRangeCount > 0)
  if (!successfulWithWarnings && !['code_summary_incomplete', 'all_records_out_of_range'].includes(run.safety_gate_code || '')) return null
  return <Alert
    type={successfulWithWarnings ? 'success' : 'warning'}
    showIcon
    message={run.safety_gate_code === 'all_records_out_of_range' ? '汇总未提交：来源记录不在所选日期内' : successfulWithWarnings ? '汇总已提交，部分来源记录已跳过或地点待分类' : '汇总未提交：来源数据未通过完整性检查'}
    description={<div className="grid gap-2">
      <div>{successfulWithWarnings ? '超出所选日期的来源记录已跳过；未分类地点仍计入汇总。以下仅为脱敏统计：' : '自动提交已跳过，本次不会覆盖现有汇总。以下仅为脱敏统计：'}</div>
      <div className="grid gap-1">
        {sourceCounts.map(([source, counts]) => <div key={source}>
          <strong>{CODE_SUMMARY_SOURCE_LABELS[source] || source}</strong>
          {`：共 ${counts.raw_count ?? 0} 条，有效 ${counts.valid_count ?? 0} 条，日期范围外 ${counts.out_of_range_count ?? 0} 条，记录身份无效 ${counts.excluded_record_count ?? 0} 条，地点未分类 ${counts.unclassified_location_count ?? 0} 条，时间无效 ${counts.invalid_time_count ?? 0} 条。`}
        </div>)}
      </div>
      <div>{successfulWithWarnings ? '地点未分类可到“平安码/管家码汇总”的“地点分类”维护；日期范围外记录不参与本次统计。' : '检查所选业务日期与来源数据；地点未分类请到码汇总的“地点分类”维护，身份或时间无效需由来源数据负责人核对。修正后再重新运行。'}</div>
    </div>}
  />
}

function previousBusinessDate(timezone: string) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone || 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date())
  const current = `${parts.find(part => part.type === 'year')?.value}-${parts.find(part => part.type === 'month')?.value}-${parts.find(part => part.type === 'day')?.value}`
  return dayjs(current).subtract(1, 'day')
}

export default function ExternalAcquisitionWorkbench() {
  const { user, environment, systemTimezone } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const [catalog, setCatalog] = useState<ExternalAcquisitionCatalogItem[]>([])
  const [selectedKey, setSelectedKey] = useState('')
  const [runs, setRuns] = useState<ExternalAcquisitionRun[]>([])
  const [loading, setLoading] = useState(true)
  const [runLoading, setRunLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [manualDateRange, setManualDateRange] = useState<[Dayjs, Dayjs]>([
    dayjs().subtract(1, 'day'), dayjs().subtract(1, 'day'),
  ])
  const [draft, setDraft] = useState({ enabled: false, cron_expr: '0 2 * * *', timezone: systemTimezone || 'Asia/Shanghai', params: {} as Record<string, any>, revision: 1 })
  const admin = isSuperAdmin(user)
  const selected = useMemo(() => catalog.find(item => item.source_key === selectedKey) || null, [catalog, selectedKey])

  const loadCatalog = async () => {
    setLoading(true)
    try {
      const items = await getExternalAcquisitionCatalog()
      setCatalog(items)
      const requested = searchParams.get('source') || SOURCE_ALIASES[searchParams.get('panel') || ''] || ''
      setSelectedKey(items.some(item => item.source_key === requested) ? requested : items[0]?.source_key || '')
      setError('')
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '来源目录加载失败')
    } finally { setLoading(false) }
  }
  useEffect(() => { void loadCatalog() }, [])
  useEffect(() => {
    if (!selected) return
    const schedule = selected.schedule
    setDraft({ enabled: Boolean(schedule?.enabled), cron_expr: schedule?.cron_expr || '0 2 * * *', timezone: schedule?.timezone || systemTimezone || 'Asia/Shanghai', params: schedule?.params || {}, revision: schedule?.revision || 1 })
    const defaultDate = previousBusinessDate(systemTimezone || 'Asia/Shanghai')
    setManualDateRange([defaultDate, defaultDate])
    void getExternalAcquisitionRuns(selected.source_key, 20).then(setRuns).catch(() => setRuns([]))
  }, [selected?.source_key, selected?.schedule?.revision])
  const hasActiveRuns = hasActiveExternalAcquisitionRun(runs)
  useEffect(() => {
    if (!selected || !hasActiveRuns) return
    let disposed = false
    let refreshing = false
    const refreshRuns = async () => {
      if (refreshing) return
      refreshing = true
      try {
        const latestRuns = await getExternalAcquisitionRuns(selected.source_key, 20)
        if (disposed) return
        setRuns(latestRuns)
        if (latestRuns[0]) {
          setCatalog(current => current.map(item => item.source_key === selected.source_key
            ? { ...item, latest_run: latestRuns[0] }
            : item))
        }
      } catch {
        // Keep the last known state; the manual refresh remains available.
      } finally {
        refreshing = false
      }
    }
    const timer = window.setInterval(() => void refreshRuns(), EXTERNAL_RUN_POLL_INTERVAL_MS)
    return () => {
      disposed = true
      window.clearInterval(timer)
    }
  }, [selected?.source_key, hasActiveRuns])
  const choose = (sourceKey: string) => {
    setSelectedKey(sourceKey)
    const next = new URLSearchParams(searchParams); next.set('tab', 'external'); next.set('source', sourceKey); next.delete('panel'); setSearchParams(next, { replace: true })
  }
  const runNow = async () => {
    if (!selected) return
    const params: Record<string, any> = {}
    if (selected.params_schema === 'date_range') {
      const [startDate, endDate] = manualDateRange
      if (startDate.isAfter(endDate, 'day')) {
        message.error('开始日期不能晚于结束日期')
        return
      }
      if (endDate.diff(startDate, 'day') + 1 > MAX_MANUAL_RANGE_DAYS) {
        message.error(`单次最多读取 ${MAX_MANUAL_RANGE_DAYS} 天，请缩短日期范围`)
        return
      }
      params.start_date = startDate.format('YYYY-MM-DD')
      params.end_date = endDate.format('YYYY-MM-DD')
    }
    setRunLoading(true)
    try {
      const result = await runExternalAcquisitionSource(selected.source_key, params)
      setRuns(current => [result.data, ...current.filter(item => item.id !== result.data.id)].slice(0, 20))
      message.success(result.reused ? '已有相同来源任务，已展示原任务' : '来源读取已启动')
    } catch (reason: any) { message.error(reason?.response?.data?.detail || '来源读取启动失败') } finally { setRunLoading(false) }
  }
  const save = async () => {
    if (!selected?.schedule || !admin) return
    setSaving(true)
    try {
      const next = await updateExternalAcquisitionSchedule(selected.source_key, draft)
      setCatalog(current => current.map(item => item.source_key === selected.source_key ? { ...item, schedule: next } : item))
      setDraft(current => ({ ...current, revision: next.revision })); message.success('自动调度已保存')
    } catch (reason: any) {
      if (reason?.response?.status === 409) { message.error('调度已被其他管理员修改，请刷新后重试'); void loadCatalog() } else message.error(reason?.response?.data?.detail?.message || reason?.response?.data?.detail || '调度保存失败')
    } finally { setSaving(false) }
  }
  const retry = async (run: ExternalAcquisitionRun) => {
    try { const result = await retryExternalAcquisitionRun(run.id); setRuns(current => [result.data, ...current.filter(item => item.id !== result.data.id)].slice(0, 20)); message.success('重试已启动') } catch (reason: any) { message.error(reason?.response?.data?.detail || '重试启动失败') }
  }
  if (loading) return <div className="py-8 text-sm text-[var(--app-text-secondary)]">正在读取外部来源目录</div>
  if (error) return <Alert type="error" showIcon message={error} action={<Button onClick={() => void loadCatalog()}>重试</Button>} />
  if (!catalog.length) return <Empty description="当前没有可用的外部来源" />
  const grouped = catalog.reduce<Record<string, ExternalAcquisitionCatalogItem[]>>((all, item) => { ;(all[item.category] ||= []).push(item); return all }, {})

  return <div className="external-acquisition-workbench grid min-h-0 gap-4 lg:grid-cols-[280px_minmax(0,1fr)]">
    <aside className={`${selectedKey ? 'hidden lg:grid' : 'grid'} content-start gap-4 rounded-md border border-[var(--app-border)] p-3`}><div><strong>来源目录</strong><div className="mt-1 text-xs text-[var(--app-text-secondary)]">选择来源后在右侧工作台操作</div></div>
      {Object.entries(grouped).map(([category, items]) => <section key={category} className="grid gap-2"><div className="text-xs font-medium text-[var(--app-text-secondary)]">{category}</div>{items.map(item => <button key={item.source_key} type="button" onClick={() => choose(item.source_key)} className={`grid gap-1 rounded-md border p-3 text-left transition ${selectedKey === item.source_key ? 'border-[var(--app-primary)] bg-[var(--app-surface-muted)]' : 'border-[var(--app-border)]'}`}><div className="flex items-center justify-between gap-2"><strong className="truncate">{item.name}</strong>{item.schedule?.enabled && <Tag color="blue">已启用</Tag>}</div><div className="text-xs text-[var(--app-text-secondary)]">{runLabel(item.latest_run)}{item.latest_run?.finished_at ? ` · ${formatUTCTime(item.latest_run.finished_at, systemTimezone)}` : ''}</div>{item.schedule?.next_run_at && <div className="text-xs text-[var(--app-text-secondary)]">下次：{formatUTCTime(item.schedule.next_run_at, systemTimezone)}</div>}</button>)}</section>)}
    </aside>
    <main className={`${selectedKey ? 'grid' : 'hidden lg:grid'} content-start gap-4 min-w-0`}>{!selected ? <Empty description="请选择来源" /> : <>
      <Button className="justify-self-start lg:hidden" onClick={() => { setSelectedKey(''); const next = new URLSearchParams(searchParams); next.delete('source'); next.delete('panel'); setSearchParams(next, { replace: true }) }}>返回来源目录</Button>
      <section className="grid gap-3 rounded-md border border-[var(--app-border)] p-4"><div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="m-0 text-lg">{selected.name}</h2><p className="mt-1 text-sm text-[var(--app-text-secondary)]">{selected.description}</p></div><Tag>{selected.source_key === 'code_summary' ? '未分类地点可先汇总' : selected.supports_auto_commit ? '完整成功才提交' : '只读/需人工确认'}</Tag></div><div className="flex flex-wrap items-center gap-3 text-sm"><span>最近结果：<strong>{runLabel(selected.latest_run)}</strong></span>{selected.latest_run?.error_code && <Tag color="red">{selected.latest_run.error_code}</Tag>}{selected.latest_run?.auto_commit_status && <span>自动提交：{selected.latest_run.auto_commit_status}</span>}</div>{selected.latest_run?.error_message && <Alert type="warning" showIcon message={selected.latest_run.error_message} />}{selected.params_schema === 'date_range' && <div className="grid justify-items-start gap-1"><label className="grid gap-1 text-sm"><span>本次读取的业务日期范围</span><RangePicker value={manualDateRange} allowClear={false} placeholder={['开始日期', '结束日期']} onChange={value => { if (value?.[0] && value[1]) setManualDateRange([value[0], value[1]]) }} /></label><span className="text-xs text-[var(--app-text-secondary)]">默认读取前一业务日；单次最多 31 天。此范围只用于本次手动读取。</span></div>}
        {selected.source_key === 'txdocs_monitor' ? (
          admin && environment === 'production' ? (
            <Button type="primary" className="justify-self-start" onClick={() => setSearchParams({ tab: 'external', panel: 'txdocs' })}>
              打开监控配置
            </Button>
          ) : (
            <span className="text-sm text-[var(--app-text-secondary)]">请使用正式环境的超级管理员账号配置腾讯只读监控。</span>
          )
        ) : (
          <Button type="primary" loading={runLoading} disabled={!selected.accessible || !selected.supports_manual} onClick={() => void runNow()}>
            立即读取/运行
          </Button>
        )}
      </section>
      {selected.supports_schedule && <section className="grid gap-3 rounded-md border border-[var(--app-border)] p-4"><div className="flex flex-wrap items-center justify-between gap-3"><div><h3 className="m-0">自动调度</h3><div className="mt-1 text-xs text-[var(--app-text-secondary)]">默认关闭；系统时区：{draft.timezone}</div></div>{!admin && <Tag>仅超级管理员可修改</Tag>}</div><div className="flex flex-wrap items-center gap-3"><Switch checked={draft.enabled} disabled={!admin} onChange={enabled => setDraft(current => ({ ...current, enabled }))} /><span>{draft.enabled ? '已启用' : '已停用'}</span><Select className="w-44" value={COMMON_CRONS.some(item => item.value === draft.cron_expr) ? draft.cron_expr : undefined} placeholder="常用时间" disabled={!admin} options={COMMON_CRONS} onChange={cron_expr => setDraft(current => ({ ...current, cron_expr }))} /><Input className="min-w-[220px] flex-1" value={draft.cron_expr} disabled={!admin} onChange={event => setDraft(current => ({ ...current, cron_expr: event.target.value }))} placeholder="高级 Cron：分 时 日 月 周" /></div>{selected.params_schema === 'date_range' && selected.source_key !== 'code_summary' && <div className="flex flex-wrap gap-3"><label className="grid gap-1 text-sm"><span>开始日期</span><Input type="date" value={draft.params.start_date || ''} disabled={!admin} onChange={event => setDraft(current => ({ ...current, params: { ...current.params, start_date: event.target.value } }))} /></label><label className="grid gap-1 text-sm"><span>结束日期</span><Input type="date" value={draft.params.end_date || ''} disabled={!admin} onChange={event => setDraft(current => ({ ...current, params: { ...current.params, end_date: event.target.value } }))} /></label></div>}{selected.source_key === 'code_summary' && <label className="grid gap-1 text-sm"><span>自动任务读取日期</span><Select className="w-56" value={draft.params.days_ago} placeholder={draft.params.start_date ? "沿用已保存的固定日期" : "前一天（默认）"} disabled={!admin} options={Array.from({ length: 31 }, (_, daysAgo) => ({ value: daysAgo, label: daysAgo === 0 ? "当天" : daysAgo === 1 ? "前一天" : daysAgo === 2 ? "前两天" : `${daysAgo} 天前` }))} onChange={days_ago => setDraft(current => ({ ...current, params: { days_ago } }))} /><span className="text-xs text-[var(--app-text-secondary)]">只影响自动调度；手动读取日期范围保持独立。</span></label>}{admin ? <Button loading={saving} onClick={() => void save()}>保存调度</Button> : <div className="text-sm text-[var(--app-text-secondary)]">当前账号可以查看计划状态；如需调整请联系超级管理员。</div>}</section>}
      <section className="grid gap-3 rounded-md border border-[var(--app-border)] p-4"><div className="flex items-center justify-between gap-2"><h3 className="m-0">最近 20 次运行</h3><Button size="small" onClick={() => void getExternalAcquisitionRuns(selected.source_key, 20).then(setRuns)}>刷新</Button></div>{runs.length ? <div className="grid gap-2">{runs.map(run => <div key={run.id} className="grid gap-2 rounded-md border border-[var(--app-border)] p-3 sm:grid-cols-[1fr_auto] sm:items-center"><div className="grid gap-2"><div><div className="flex flex-wrap items-center gap-2"><strong>#{run.id}</strong><Tag color={run.status === 'success' ? 'green' : run.status === 'failed' ? 'red' : 'gold'}>{runLabel(run)}</Tag>{run.safety_gate_code && <Tag>{run.safety_gate_code}</Tag>}</div><div className="mt-1 text-xs text-[var(--app-text-secondary)]">{run.created_at ? formatUTCTime(run.created_at, systemTimezone) : ''} · {run.phase}</div>{run.message && <div className="mt-1 text-xs text-[var(--app-text-secondary)]">{run.message}</div>}{run.status === 'running' && run.progress !== null && <Progress percent={run.progress} size="small" status="active" />}</div><CodeSummaryQuality run={run} /></div>{run.status === 'failed' && <Button size="small" onClick={() => void retry(run)}>重试</Button>}</div>)}</div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚无运行记录" />}</section>
    </>}</main>
  </div>
}
