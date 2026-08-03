import { ReloadOutlined, RightOutlined } from '@ant-design/icons'
import { Alert, Button, Segmented, Skeleton } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  getMobileTaskHome,
  type MobileTaskHomeData,
  type MobileTaskScope,
} from '../api/client'
import { sortMobileTaskBusinesses } from '../utils/mobileTasks'

function metricValue(value: number | null, snapshotAvailable: boolean) {
  if (!snapshotAvailable || value === null) return '—'
  return String(value)
}

function formatSyncTime(value: string | null) {
  if (!value) return '尚无成功同步'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

export default function MobileTaskHome() {
  const navigate = useNavigate()
  const [scope, setScope] = useState<MobileTaskScope>('mine')
  const [data, setData] = useState<MobileTaskHomeData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setData(await getMobileTaskHome(scope))
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || reason?.message || '任务首页读取失败')
    } finally {
      setLoading(false)
    }
  }, [scope])

  useEffect(() => { void load() }, [load])

  if (loading && !data) {
    return <div className="app-card p-5"><Skeleton active paragraph={{ rows: 8 }} /></div>
  }

  return (
    <div className="mobile-task-page">
      {error && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={() => void load()}>重试</Button>} />}

      {data && (
        <>
          <section className="mobile-task-hero">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-sm text-white/75">{data.person.community}社区 · {data.person.position}</div>
                <h1 className="mt-1 text-xl font-semibold text-white">{data.person.name}，今天待核查</h1>
              </div>
              <Button
                type="text"
                aria-label="刷新任务首页"
                icon={<ReloadOutlined />}
                className="mobile-task-hero__refresh"
                loading={loading}
                onClick={() => void load()}
              />
            </div>

            <div className="mt-5 flex items-end gap-2">
              <strong className="text-5xl font-semibold leading-none text-white">{data.personal.pending}</strong>
              <span className="pb-1 text-sm text-white/70">条我的任务</span>
            </div>

            <div className="mobile-task-hero__metrics">
              <div><span>今日新下发</span><strong>{metricValue(data.personal.new_today, data.daily_snapshot_available)}</strong></div>
              <div><span>昨日结转</span><strong>{metricValue(data.personal.carryover_today, data.daily_snapshot_available)}</strong></div>
              <div><span>今日已完成</span><strong>{metricValue(data.personal.completed_today, data.daily_snapshot_available)}</strong></div>
            </div>

            <div className="mobile-task-hero__footer">
              <span>本社区待核查 {data.community.pending} 条</span>
              <span>最近同步 {formatSyncTime(data.last_success_at)}</span>
            </div>
          </section>

          {!data.daily_snapshot_available && (
            <Alert
              type="info"
              showIcon
              message="今天尚无同步快照"
              description="待核查读取当前在线来源；新下发、结转和今日完成将在同步后显示。"
            />
          )}

          <section>
            <div className="mb-3 flex items-center justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold text-[var(--app-text-strong)]">业务待办</h2>
                <p className="mt-0.5 text-xs text-[var(--app-text-secondary)]">
                  {scope === 'mine' ? '只看分配给我的任务' : `查看${data.person.community}社区全部任务`}
                </p>
              </div>
              <Segmented
                className="mobile-task-scope-switch"
                value={scope}
                onChange={value => setScope(value as MobileTaskScope)}
                options={[{ label: '我的', value: 'mine' }, { label: '本社区', value: 'community' }]}
              />
            </div>

            <div className="mobile-task-business-list">
              {sortMobileTaskBusinesses(data.businesses).map(item => (
                <button
                  key={item.parser_type}
                  type="button"
                  className={`mobile-task-business-card${item.pending === 0 ? ' is-empty' : ''}`}
                  onClick={() => navigate(`/tasks?type=${encodeURIComponent(item.parser_type)}&scope=${scope}`)}
                >
                  <div className="min-w-0 flex-1 text-left">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold text-[var(--app-text-strong)]">{item.label}</span>
                      {!item.source_ready && <span className="mobile-task-badge is-warning">等待同步</span>}
                      {item.review > 0 && <span className="mobile-task-badge is-danger">异常来源 {item.review}</span>}
                    </div>
                    <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-[var(--app-text-secondary)]">
                      <span>未开始 {item.unchecked}</span>
                      <span>待补结果 {item.checked}</span>
                      <span>已完成 {item.completed}</span>
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <strong className="text-2xl font-semibold text-[var(--app-primary)]">{item.pending}</strong>
                    <RightOutlined className="text-[var(--app-text-muted)]" />
                  </div>
                </button>
              ))}
            </div>
          </section>
        </>
      )}
    </div>
  )
}
