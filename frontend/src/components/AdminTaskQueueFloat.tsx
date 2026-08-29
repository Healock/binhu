import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Collapse,
  Drawer,
  Empty,
  FloatButton,
  Progress,
  Skeleton,
  Tag,
} from 'antd'
import {
  CloudServerOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons'
import {
  getAdminTaskQueue,
  getAdminTaskQueueDetails,
  type AdminTaskQueueDetailItem,
  type AdminTaskQueueItem,
  type AdminTaskQueueResponse,
  type AdminTaskQueueState,
} from '../api/client'
import { useAuth } from '../context/AuthContext'
import useMobileViewport from '../hooks/useMobileViewport'
import useSystemTime from '../hooks/useSystemTime'
import type { User } from '../types'

const CLOSED_REFRESH_MS = 30_000
const OPEN_REFRESH_MS = 10_000

const STATE_META: Record<AdminTaskQueueState, { label: string; color: string }> = {
  queued: { label: '排队中', color: 'blue' },
  running: { label: '运行中', color: 'processing' },
  retrying: { label: '等待重试', color: 'orange' },
  success: { label: '已完成', color: 'success' },
  warning: { label: '需关注', color: 'warning' },
  failed: { label: '失败', color: 'error' },
  paused: { label: '已暂停', color: 'default' },
  cancelled: { label: '已取消', color: 'default' },
}

const PHASE_LABELS: Record<string, string> = {
  queued: '等待执行',
  starting: '正在启动',
  fetching: '正在获取',
  parsing: '正在解析',
  preparing: '正在准备',
  querying: '正在查询',
  sending: '正在发送',
  deleting: '正在归档',
  finished: '处理结束',
  registration: '真实登记',
  scan: '状态核对',
  import: '数据导入',
  backup: '数据库备份',
}

export function isAdminTaskQueueUser(user: User | null | undefined): boolean {
  if (!user) return false
  const groupCodes = user.permission_groups?.map(group => group.code) || []
  if (user.permission_group?.code) groupCodes.push(user.permission_group.code)
  return groupCodes.length > 0
    ? groupCodes.some(code => ['admin', 'super_admin'].includes(code))
    : ['admin', 'super_admin'].includes(user.role)
}

function TaskQueueDetail({
  detail,
}: {
  detail: AdminTaskQueueDetailItem
}) {
  const formatTime = useSystemTime()

  return (
    <div className="admin-task-queue-detail">
      <div className="admin-task-queue-detail__header">
        <strong>{detail.reference}</strong>
        <Tag color={detail.state === 'paused' || detail.state === 'conflict' ? 'warning' : 'orange'}>
          {detail.state === 'paused' ? '已暂停' : detail.state === 'conflict' ? '冲突' : '等待重试'}
        </Tag>
      </div>
      <div className="admin-task-queue-detail__meta">
        <span>{detail.action}</span>
        <span>错误类型：{detail.error_code || 'unknown'}</span>
        {detail.attempt_count > 0 && <span>已尝试 {detail.attempt_count} 次</span>}
      </div>
      <div className="admin-task-queue-detail__analysis">
        <div><b>原因分析</b><span>{detail.diagnosis}</span></div>
        <div><b>建议处理</b><span>{detail.recommended_action}</span></div>
      </div>
      <div className="admin-task-queue-detail__footer">
        <span>{detail.updated_at ? formatTime(detail.updated_at) : '时间待更新'}</span>
        <span className="admin-task-queue-detail__protected">
          <SafetyCertificateOutlined /> 请按建议处理
        </span>
      </div>
    </div>
  )
}

function TaskQueueCard({
  item,
  onRefresh,
}: {
  item: AdminTaskQueueItem
  onRefresh: () => void
}) {
  const formatTime = useSystemTime()
  const meta = STATE_META[item.state]
  const time = item.finished_at || item.updated_at || item.started_at || item.created_at
  const [details, setDetails] = useState<AdminTaskQueueDetailItem[]>([])
  const [detailsOpen, setDetailsOpen] = useState(false)
  const [detailsLoading, setDetailsLoading] = useState(false)
  const [detailsError, setDetailsError] = useState('')

  const loadDetails = useCallback(async () => {
    if (!item.detail_count) return
    setDetailsLoading(true)
    setDetailsError('')
    try {
      const result = await getAdminTaskQueueDetails(item.source)
      setDetails(result.data)
    } catch {
      setDetailsError('问题明细暂时无法读取，请稍后重试。')
    } finally {
      setDetailsLoading(false)
    }
  }, [item.detail_count, item.source])

  const toggleDetails = () => {
    const next = !detailsOpen
    setDetailsOpen(next)
    if (next && !details.length) void loadDetails()
  }

  return (
    <article className={`admin-task-queue-card is-${item.state}`}>
      <div className="admin-task-queue-card__header">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold text-[var(--app-text-primary)]">
            {item.title}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-[var(--app-text-tertiary)]">
            <span>{item.category}</span>
            {item.phase && <span>· {PHASE_LABELS[item.phase] || item.phase}</span>}
          </div>
        </div>
        <Tag color={meta.color}>{meta.label}</Tag>
      </div>
      {item.progress !== null && (
        <Progress
          percent={item.progress}
          size="small"
          status={item.state === 'failed' ? 'exception' : item.state === 'success' ? 'success' : 'active'}
          format={() => item.total !== null
            ? `${item.current ?? 0}/${item.total}`
            : `${item.progress}%`}
        />
      )}
      {item.message && (
        <div className="text-xs leading-5 text-[var(--app-text-secondary)]">{item.message}</div>
      )}
      <div className="text-[11px] text-[var(--app-text-tertiary)]">
        {time ? formatTime(time) : '时间待更新'}
      </div>
      {item.detail_count > 0 && (
        <Collapse
          ghost
          className="admin-task-queue-card__details"
          activeKey={detailsOpen ? ['details'] : []}
          onChange={toggleDetails}
          items={[{
            key: 'details',
            label: `查看问题明细（${item.detail_count}）`,
            children: (
              <div className="admin-task-queue-detail-list">
                {detailsLoading && <Skeleton active paragraph={{ rows: 3 }} />}
                {detailsError && (
                  <Alert
                    type="warning"
                    showIcon
                    message={detailsError}
                    action={<Button size="small" onClick={() => void loadDetails()}>重试</Button>}
                  />
                )}
                {!detailsLoading && !detailsError && details.map(detail => (
                  <TaskQueueDetail key={detail.id} detail={detail} />
                ))}
                {!detailsLoading && !detailsError && !details.length && (
                  <div className="admin-task-queue-empty">问题已经处理完毕</div>
                )}
              </div>
            ),
          }]}
        />
      )}
    </article>
  )
}

export default function AdminTaskQueueFloat() {
  const { user } = useAuth()
  const mobile = useMobileViewport()
  const [open, setOpen] = useState(false)
  const [data, setData] = useState<AdminTaskQueueResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const allowed = isAdminTaskQueueUser(user)

  const refresh = useCallback(async (showLoading = false) => {
    if (!allowed || document.visibilityState !== 'visible') return
    if (showLoading) setLoading(true)
    try {
      const result = await getAdminTaskQueue()
      setData(result)
      setError('')
    } catch {
      setError('任务队列暂时无法读取，请稍后重试。')
    } finally {
      if (showLoading) setLoading(false)
    }
  }, [allowed])

  useEffect(() => {
    if (!allowed) return
    void refresh(true)
    const interval = window.setInterval(
      () => void refresh(false),
      open ? OPEN_REFRESH_MS : CLOSED_REFRESH_MS,
    )
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') void refresh(false)
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      window.clearInterval(interval)
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [allowed, open, refresh])

  const activeItems = useMemo(
    () => data?.items.filter(item => item.active) || [],
    [data],
  )
  const recentItems = useMemo(
    () => data?.items.filter(item => !item.active) || [],
    [data],
  )

  if (!allowed) return null

  return (
    <>
      <FloatButton
        rootClassName="admin-task-queue-float"
        type={data?.active_count ? 'primary' : 'default'}
        shape="circle"
        icon={<CloudServerOutlined />}
        badge={{ count: data?.active_count || 0, overflowCount: 99 }}
        tooltip="后台任务队列"
        aria-label={`打开后台任务队列，当前 ${data?.active_count || 0} 项活动任务`}
        onClick={() => setOpen(true)}
      />
      <Drawer
        title="后台任务队列"
        placement={mobile ? 'bottom' : 'right'}
        width={mobile ? undefined : 440}
        height={mobile ? 'min(82vh, 680px)' : undefined}
        open={open}
        onClose={() => setOpen(false)}
        extra={(
          <Button
            type="text"
            icon={<ReloadOutlined />}
            loading={loading}
            onClick={() => void refresh(true)}
          >
            刷新
          </Button>
        )}
      >
        <div className="admin-task-queue-panel">
          <div className="admin-task-queue-summary">
            <div><strong>{data?.running_count || 0}</strong><span>运行中</span></div>
            <div><strong>{data?.queued_count || 0}</strong><span>排队中</span></div>
            <div><strong>{data?.attention_count || 0}</strong><span>需关注</span></div>
          </div>

          {error && (
            <Alert
              type="warning"
              showIcon
              message={error}
              action={<Button size="small" onClick={() => void refresh(true)}>重试</Button>}
            />
          )}
          {!!data?.unavailable_sources.length && (
            <Alert
              type="info"
              showIcon
              message={`部分队列暂不可用：${data.unavailable_sources.join('、')}`}
            />
          )}

          {loading && !data ? (
            <Skeleton active paragraph={{ rows: 7 }} />
          ) : !data?.items.length ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前没有后台任务" />
          ) : (
            <>
              <section className="admin-task-queue-section">
                <div className="admin-task-queue-section__title">
                  <span>当前任务</span>
                  <Tag>{activeItems.length}</Tag>
                </div>
                {activeItems.length
                  ? activeItems.map(item => (
                    <TaskQueueCard key={item.id} item={item} onRefresh={() => void refresh(false)} />
                  ))
                  : <div className="admin-task-queue-empty">当前没有正在运行或排队的任务</div>}
              </section>
              {!!recentItems.length && (
                <section className="admin-task-queue-section">
                  <div className="admin-task-queue-section__title">
                    <span>最近结束</span>
                    <Tag>{recentItems.length}</Tag>
                  </div>
                  {recentItems.map(item => (
                    <TaskQueueCard key={item.id} item={item} onRefresh={() => void refresh(false)} />
                  ))}
                </section>
              )}
            </>
          )}
        </div>
      </Drawer>
    </>
  )
}
