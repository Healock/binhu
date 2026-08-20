import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  Alert,
  Button,
  Progress,
  Skeleton,
  Tag,
  Tooltip,
} from 'antd'
import {
  BellOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  ReloadOutlined,
  RightOutlined,
  SyncOutlined,
  UserOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import {
  formatUTCTime,
  getRoleDashboard,
  type RoleDashboardData,
} from '../api/client'
import { Panel } from '../components/ui'
import HiddenWorkspaceOverlay from '../components/HiddenWorkspaceOverlay'
import { useAuth } from '../context/AuthContext'
import {
  DASHBOARD_CACHE_FRESH_MS,
  readRoleDashboardCache,
  writeRoleDashboardCache,
} from '../utils/dashboardCache'
import { formatDashboardIdentityContext } from '../utils/dashboardIdentity'

const percent = (value: unknown) => `${(Number(value || 0) * 100).toFixed(0)}%`
const metric = (value: unknown, fallback = '0') => (
  value === null || value === undefined ? fallback : String(value)
)

function buildUrl(path: string, params: Record<string, string | number | null | undefined>) {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== '') search.set(key, String(value))
  })
  const query = search.toString()
  return query ? `${path}?${query}` : path
}

function DashboardCard({
  label,
  value,
  hint,
  tone = 'blue',
  onClick,
  onHintClick,
}: {
  label: string
  value: string | number
  hint?: string
  tone?: 'blue' | 'green' | 'amber' | 'red' | 'purple' | 'slate'
  onClick?: () => void
  onHintClick?: () => void
}) {
  const content = (
    <>
      <span className={`role-dashboard-metric__icon role-dashboard-metric__icon--${tone}`} aria-hidden="true">
        {tone === 'green' ? <CheckCircleOutlined /> : tone === 'amber' || tone === 'red' ? <WarningOutlined /> : <ClockCircleOutlined />}
      </span>
      <span className="role-dashboard-metric__body">
        <span className="role-dashboard-metric__label">{label}</span>
        <strong className="role-dashboard-metric__value">{value}</strong>
        {hint && (onHintClick ? (
          <button
            type="button"
            className="role-dashboard-metric__hint role-dashboard-metric__hint--secret"
            aria-label={`${label}${hint}`}
            onClick={(event) => {
              event.stopPropagation()
              onHintClick()
            }}
          >
            {hint}
          </button>
        ) : <span className="role-dashboard-metric__hint">{hint}</span>)}
      </span>
      {onClick && <RightOutlined className="role-dashboard-metric__arrow" />}
    </>
  )
  return onClick ? (
    <button type="button" className="role-dashboard-metric" onClick={onClick}>{content}</button>
  ) : (
    <div className="role-dashboard-metric">{content}</div>
  )
}

function MetricGrid({ children }: { children: ReactNode }) {
  return <div className="role-dashboard-metric-grid">{children}</div>
}

function SnapshotHint({ available }: { available: boolean }) {
  if (available) return null
  return <Alert type="info" showIcon message="今日尚无同步快照，今日新下发、结转和完成暂不显示为 0。" />
}

function ContributionPanel({ data }: { data: RoleDashboardData }) {
  const navigate = useNavigate()
  const [hiddenWorkspaceOpen, setHiddenWorkspaceOpen] = useState(false)
  const secretClicks = useRef({ count: 0, lastAt: 0 })
  const maximum = Math.max(...data.contribution.days.map(item => item.count), 1)
  const handleSecretClick = () => {
    const now = Date.now()
    const clicks = secretClicks.current
    if (now - clicks.lastAt > 2500) clicks.count = 0
    clicks.count += 1
    clicks.lastAt = now
    if (clicks.count >= 10) {
      clicks.count = 0
      setHiddenWorkspaceOpen(true)
    }
  }
  return (
    <>
      <Panel
        title="近 7 日个人贡献"
        description="只统计平台记录的实际工作，不包含浏览、查询等普通操作"
        extra={<Button type="link" onClick={() => navigate(`/people/${data.contribution.profile_user_id}`, { state: { returnTo: '/', returnLabel: '返回仪表盘' } })}>完整个人资料</Button>}
      >
        <MetricGrid>
          <DashboardCard label="实际工作" value={data.contribution.total} hint="次" tone="purple" onHintClick={handleSecretClick} />
          <DashboardCard label="活跃天数" value={data.contribution.active_days} hint="天" tone="blue" />
          <DashboardCard label="连续工作" value={data.contribution.longest_streak} hint="天" tone="green" />
        </MetricGrid>
        <div className="role-dashboard-activity" aria-label="近 7 日个人工作量">
          {data.contribution.days.map(item => (
            <Tooltip key={item.date} title={`${item.date}：${item.count} 次`}>
              <div className="role-dashboard-activity__day">
                <span
                  className="role-dashboard-activity__bar"
                  style={{ height: `${Math.max(8, Math.round(item.count / maximum * 64))}px` }}
                />
                <span>{item.date.slice(5)}</span>
              </div>
            </Tooltip>
          ))}
        </div>
      </Panel>
      <HiddenWorkspaceOverlay open={hiddenWorkspaceOpen} onClose={() => setHiddenWorkspaceOpen(false)} />
    </>
  )
}

function FlowTasks({ data }: { data: RoleDashboardData }) {
  const navigate = useNavigate()
  const flow = data.flow_tasks
  if (!flow) return null
  if (!flow.available) return <Alert type="warning" showIcon message={flow.message || '当前人员配置不完整，暂时无法读取任务。'} />
  const personal = flow.personal!
  const todayParams = { start: data.business_date, end: data.business_date, type: '总汇总表' }
  return (
    <>
      <Panel title="我的任务" description="优先处理待核查和需复核任务">
        <SnapshotHint available={Boolean(flow.daily_snapshot_available)} />
        <MetricGrid>
          <DashboardCard label="我的待核查" value={personal.pending} tone="red" onClick={() => navigate('/tasks?scope=mine&status=pending')} />
          <DashboardCard label="需复核" value={personal.review} tone="amber" onClick={() => navigate('/tasks?scope=mine&status=review')} />
          <DashboardCard label="今日新下发" value={metric(personal.new_today, '—')} tone="blue" onClick={personal.new_today == null ? undefined : () => navigate(buildUrl('/summary', { ...todayParams, category: 'new', scope: 'responsibility' }))} />
          <DashboardCard label="昨日结转" value={metric(personal.carryover_today, '—')} tone="amber" onClick={personal.carryover_today == null ? undefined : () => navigate(buildUrl('/summary', { ...todayParams, category: 'carryover', scope: 'responsibility' }))} />
          <DashboardCard label="今日完成" value={metric(personal.completed_today, '—')} tone="green" onClick={personal.completed_today == null ? undefined : () => navigate(buildUrl('/summary', { ...todayParams, category: 'completed', scope: 'responsibility' }))} />
          {flow.community_totals && (
            <DashboardCard label="本社区待办" value={flow.community_totals.pending} tone="purple" onClick={() => navigate('/tasks?scope=community&status=pending')} />
          )}
        </MetricGrid>
      </Panel>

      <Panel title="五类业务待办" description="0 条业务自动沉底，点击直接进入对应任务列表">
        <div className="role-dashboard-business-list">
          {flow.businesses.map(item => (
            <button
              type="button"
              key={item.parser_type}
              className={`role-dashboard-business${item.pending === 0 ? ' is-empty' : ''}`}
              onClick={() => navigate(buildUrl('/tasks', { type: item.parser_type, scope: 'mine', status: 'pending' }))}
            >
              <span className="role-dashboard-business__name">{item.label}</span>
              <span className="role-dashboard-business__meta">
                <strong>{item.pending}</strong> 待办 · {item.review} 需复核
              </span>
              <RightOutlined />
            </button>
          ))}
        </div>
      </Panel>
    </>
  )
}

function OnlineOverview({ data }: { data: RoleDashboardData }) {
  const navigate = useNavigate()
  const overview = data.online_overview
  if (!overview) return null
  const week = overview.week || {}
  const go = (category: string) => navigate(buildUrl('/summary', {
    start: data.period.start_date,
    end: data.period.end_date,
    type: '总汇总表',
    category,
    scope: 'responsibility',
  }))
  return (
    <Panel title="在线核查态势" description={`${overview.scope_label} · ${data.period.start_date} 至 ${data.period.end_date}`} extra={<Button type="link" onClick={() => navigate(buildUrl('/summary', { start: data.period.start_date, end: data.period.end_date, scope: 'responsibility' }))}>查看完整汇总</Button>}>
      <MetricGrid>
        <DashboardCard label="任务总数" value={metric(week.total_tasks)} tone="blue" />
        <DashboardCard label="待完成" value={metric(week.pending_tasks)} tone="red" onClick={() => go('pending')} />
        <DashboardCard label="最终完成" value={metric(week.completed_tasks)} tone="green" onClick={() => go('completed')} />
        <DashboardCard label="当前无法核实" value={metric(week.unable_to_verify)} tone="amber" />
        <DashboardCard label="完成率" value={percent(week.completion_rate)} tone="purple" />
      </MetricGrid>
      {overview.community_breakdown.length > 0 && (
        <div className="role-dashboard-community-list">
          {overview.community_breakdown.slice(0, 8).map(item => (
            <div key={item.community} className="role-dashboard-community">
              <div className="flex items-center justify-between gap-3">
                <strong>{item.community}</strong>
                <span>{item.completed}/{item.total}</span>
              </div>
              <Progress percent={Math.round(item.completion_rate * 100)} showInfo={false} size="small" />
            </div>
          ))}
        </div>
      )}
    </Panel>
  )
}

function VisitOverview({ data }: { data: RoleDashboardData }) {
  const navigate = useNavigate()
  const visit = data.visit_overview
  if (!visit) return null
  const isSelfOwned = visit.category === 'self_owned'
  const todayVisits = Number(visit.today.visit_records ?? visit.today.visits ?? 0)
  const weekVisits = Number(visit.week.visit_records ?? visit.week.visits ?? 0)
  const changes = Number(visit.week.total_changes ?? 0)
  const unrated = Number(visit.week.unrated_records ?? 0)
  const route = buildUrl('/visit-summary', {
    start: data.period.start_date,
    end: data.period.end_date,
    category: visit.category,
    scope: 'responsibility',
  })
  return (
    <Panel title={isSelfOwned ? '自购房走访工作台' : '走访概览'} description={visit.scope_label || '本人职责范围'} extra={<Button type="link" onClick={() => navigate(route)}>查看走访汇总</Button>}>
      <MetricGrid>
        <DashboardCard label="今日走访" value={todayVisits} tone="blue" onClick={() => navigate(buildUrl('/visit-summary', { start: data.business_date, end: data.business_date, category: visit.category, scope: 'responsibility' }))} />
        <DashboardCard label="近 7 日走访" value={weekVisits} tone="purple" onClick={() => navigate(route)} />
        <DashboardCard label="近 7 日变动" value={changes} tone="green" onClick={() => navigate(route)} />
        <DashboardCard label="数据待补" value={unrated} tone={unrated ? 'amber' : 'slate'} onClick={() => navigate(route)} />
      </MetricGrid>
    </Panel>
  )
}

function DispatchOverview({ data }: { data: RoleDashboardData }) {
  const navigate = useNavigate()
  const batch = data.dispatch_overview?.active_batch
  if (!data.dispatch_overview) return null
  if (!batch) return <Panel title="下发任务"><div className="role-dashboard-empty">当前没有未完成批次</div></Panel>
  const go = (status: string, category?: string) => navigate(buildUrl('/police-tasks', { batch: batch.id, status, category }))
  const reviewedPercent = batch.total_count ? Math.round(batch.reviewed_count / batch.total_count * 100) : 0
  return (
    <Panel title="下发任务" description={`批次 #${batch.id} · ${batch.file_name}`} extra={<Button type="link" onClick={() => go('pending_review')}>进入共享队列</Button>}>
      <div className="mb-4">
        <div className="mb-1 flex items-center justify-between text-sm"><span>审核进度</span><strong>{batch.reviewed_count}/{batch.total_count}</strong></div>
        <Progress percent={reviewedPercent} />
      </div>
      <MetricGrid>
        <DashboardCard label="待审核" value={batch.counts.pending_review} tone="amber" onClick={() => go('pending_review')} />
        <DashboardCard label="待研判" value={batch.counts.abnormal} tone="red" onClick={() => go('pending_review', 'manual')} />
        <DashboardCard label="待发布" value={batch.counts.pending_publish} tone="blue" onClick={() => go('pending_publish')} />
        <DashboardCard label="待对账" value={batch.counts.needs_reconciliation} tone="purple" onClick={() => go('needs_reconciliation')} />
        <DashboardCard label="内容冲突" value={batch.counts.conflict} tone="red" onClick={() => go('conflict')} />
      </MetricGrid>
      {batch.community_distribution.length > 0 && (
        <div className="mt-4 flex flex-wrap gap-2">
          {batch.community_distribution.map(item => (
            <Tag key={item.community_id} bordered={false} color="blue">
              {item.community_name} {item.count} 条
            </Tag>
          ))}
        </div>
      )}
    </Panel>
  )
}

function ManagementOverview({ data }: { data: RoleDashboardData }) {
  const navigate = useNavigate()
  const management = data.management
  if (!management) return null
  const syncOk = ['success', 'completed'].includes(management.sync.status)
  return (
    <Panel title="管理提醒" description="只显示当前账号有权限查看的管理状态">
      <MetricGrid>
        <DashboardCard label="同步状态" value={syncOk ? '正常' : management.sync.status || '空闲'} tone={syncOk ? 'green' : 'amber'} />
        <DashboardCard label="在线回写" value={management.online_writeback_enabled ? '已开启' : '已关闭'} tone={management.online_writeback_enabled ? 'green' : 'red'} />
        <DashboardCard label="下发异常" value={management.dispatch_exceptions} tone={management.dispatch_exceptions ? 'red' : 'slate'} onClick={data.dispatch_overview ? () => navigate('/police-tasks?status=conflict') : undefined} />
        {management.latest_backup && <DashboardCard label="最近备份" value={management.latest_backup.status === 'success' ? '成功' : management.latest_backup.status} tone={management.latest_backup.status === 'success' ? 'green' : 'amber'} onClick={() => navigate('/operations')} />}
      </MetricGrid>
    </Panel>
  )
}

export default function RoleDashboard() {
  const { user, systemTimezone } = useAuth()
  const [data, setData] = useState<RoleDashboardData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async (showSpinner = true) => {
    if (showSpinner) setLoading(true)
    try {
      const next = await getRoleDashboard()
      setData(next)
      if (user?.id) writeRoleDashboardCache(window.sessionStorage, user.id, next)
      setError('')
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '仪表盘读取失败')
    } finally {
      setLoading(false)
    }
  }, [user?.id])

  useEffect(() => {
    if (!user?.id) return
    const cached = readRoleDashboardCache(window.sessionStorage, user.id)
    if (cached) {
      setData(cached.data)
      setLoading(false)
      if (cached.ageMs <= DASHBOARD_CACHE_FRESH_MS) return
      void load(false)
      return
    }
    setData(null)
    void load(true)
  }, [load, user?.id])

  const syncLabel = useMemo(() => {
    if (!data?.last_success_at) return '尚无成功同步记录'
    return `最后成功同步 ${formatUTCTime(data.last_success_at, systemTimezone)}`
  }, [data?.last_success_at, systemTimezone])

  if (!data && loading) {
    return <div className="app-page"><div className="app-card p-6"><Skeleton active paragraph={{ rows: 10 }} /></div></div>
  }
  if (!data) {
    return <div className="app-page"><Alert type="error" showIcon message={error || '仪表盘读取失败'} action={<Button onClick={() => void load()}>重试</Button>} /></div>
  }
  const identityContext = formatDashboardIdentityContext(
    data.identity.departments,
    data.scope.label,
    data.scope.communities,
  )

  return (
    <div className="app-page role-dashboard-page">
      <section className="role-dashboard-hero">
        <div className="role-dashboard-hero__identity">
          <span className="role-dashboard-hero__avatar"><UserOutlined /></span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1>{data.identity.display_name}</h1>
              <Tag bordered={false} color="blue">{data.identity.position}</Tag>
            </div>
            <p>{identityContext}</p>
          </div>
        </div>
        <div className="role-dashboard-hero__meta">
          <span><ClockCircleOutlined /> 业务日期 {data.business_date}</span>
          <span><SyncOutlined /> {syncLabel}</span>
          <button type="button" onClick={() => window.dispatchEvent(new Event('binhu:open-notification-center'))}>
            <BellOutlined /> {data.notifications.unread_count ? `${data.notifications.unread_count} 条未读` : '消息中心'}
          </button>
          <Button type="text" icon={<ReloadOutlined />} loading={loading} onClick={() => void load(true)}>刷新</Button>
        </div>
      </section>

      {error && <Alert type="warning" showIcon message={error} />}

      <div className="role-dashboard-sections">
        <FlowTasks data={data} />
        {data.flow_tasks && <ContributionPanel data={data} />}
        {data.identity.position === '自购房' && <VisitOverview data={data} />}
        {data.identity.position === '自购房' && <ContributionPanel data={data} />}
        {data.identity.position !== '自购房' && <OnlineOverview data={data} />}
        {data.identity.position !== '自购房' && <VisitOverview data={data} />}
        <DispatchOverview data={data} />
        {!data.flow_tasks && data.identity.position !== '自购房' && <ContributionPanel data={data} />}
        <ManagementOverview data={data} />
      </div>
    </div>
  )
}
