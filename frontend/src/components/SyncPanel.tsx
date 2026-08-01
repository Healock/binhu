import { useEffect, useMemo, useState } from 'react'
import { Alert, Button, Progress, Tag, Tooltip } from 'antd'
import {
  ClockCircleOutlined,
  DatabaseOutlined,
  SyncOutlined,
} from '@ant-design/icons'
import type { SyncStatus } from '../types'
import { formatUTCTime } from '../api/client'
import {
  formatCountdown,
  getRemainingTime,
  getServerOffset,
} from '../utils/countdown'

interface Props {
  syncing: boolean
  status: SyncStatus | null
  taskError: string | null
  statusError: string | null
  actionError: string | null
  onSync: () => void
  canManualSync: boolean
  timezone?: string
}

const statusLabel = (status?: string) => {
  if (status === 'success' || status === 'completed') return { color: 'success', label: '同步完成' }
  if (status === 'partial') return { color: 'warning', label: '部分同步失败' }
  if (status === 'failed') return { color: 'error', label: '同步失败' }
  if (status === 'running') return { color: 'processing', label: '正在同步' }
  if (status === 'pending') return { color: 'default', label: '等待处理' }
  if (status === 'conflict') return { color: 'warning', label: '已有同步任务' }
  return { color: 'default', label: '等待首次同步' }
}

const phaseLabel: Record<string, string> = {
  queued: '等待执行',
  syncing: '同步在线数据',
  building_reports: '生成汇总报表',
  finished: '任务已结束',
}

function intervalLabel(minutes?: number) {
  if (!minutes) return '-'
  if (minutes < 60) return `${minutes} 分钟`
  if (minutes % 1440 === 0) return `${minutes / 1440} 天`
  if (minutes % 60 === 0) return `${minutes / 60} 小时`
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟`
}

export default function SyncPanel({
  syncing,
  status,
  taskError,
  statusError,
  actionError,
  onSync,
  canManualSync,
  timezone = 'Asia/Shanghai',
}: Props) {
  const [clock, setClock] = useState(Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  const schedule = status?.schedule
  const serverOffset = useMemo(
    () => getServerOffset(schedule?.server_time),
    [schedule?.server_time],
  )
  const remaining = getRemainingTime(
    schedule?.next_run_at,
    serverOffset,
    clock,
  )
  const countdown = remaining == null
    ? null
    : formatCountdown(remaining)
  const progress = status && status.total_steps > 0
    ? Math.round((status.completed_steps / status.total_steps) * 100)
    : 0
  const currentStatus = statusLabel(status?.status)
  const sourceLabel = status?.trigger_source === 'scheduled' ? '自动触发' : '手动触发'

  let buttonLabel = '立即同步'
  if (syncing) {
    buttonLabel = '同步中'
  } else if (schedule?.enabled && countdown) {
    buttonLabel = canManualSync
      ? `立即同步 · 下次 ${countdown}`
      : `下次自动同步 · ${countdown}`
  } else if (!canManualSync) {
    buttonLabel = '仅管理员可同步'
  }

  return (
    <section className="app-card">
      <div className="app-toolbar items-start justify-between gap-3 py-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="app-card__title">数据同步</h2>
            <Tag color={currentStatus.color}>{currentStatus.label}</Tag>
            {status?.task_id ? <Tag>{sourceLabel}</Tag> : null}
          </div>
          <p className="app-card__description mt-1">
            {syncing
              ? status?.current_item || phaseLabel[status?.phase || 'queued']
              : schedule?.enabled
                ? `自动同步每 ${intervalLabel(schedule.interval_minutes)}执行一次`
                : '自动同步已关闭'}
          </p>
        </div>

        <Tooltip
          title={!canManualSync ? '仅管理员和超级管理员可以手动同步' : undefined}
        >
          <Button
            type={canManualSync ? 'primary' : 'default'}
            icon={<SyncOutlined spin={syncing} />}
            onClick={onSync}
            loading={syncing}
            disabled={syncing || !canManualSync}
            className="shrink-0"
          >
            {buttonLabel}
          </Button>
        </Tooltip>
      </div>

      <div className="grid gap-2 border-t border-slate-100 px-4 py-3 text-sm sm:grid-cols-3">
        <div className="min-w-0">
          <div className="text-xs text-slate-400">下一次自动同步</div>
          <div className="mt-1 flex items-center gap-1.5 font-medium text-slate-700">
            <ClockCircleOutlined />
            {schedule?.enabled && countdown ? countdown : '已关闭'}
          </div>
          {schedule?.next_run_at && schedule.enabled && (
            <div className="mt-0.5 truncate text-xs text-slate-400">
              {formatUTCTime(schedule.next_run_at, timezone)}
            </div>
          )}
        </div>
        <div className="min-w-0">
          <div className="text-xs text-slate-400">最近成功</div>
          <div className="mt-1 truncate font-medium text-slate-700">
            {status?.last_success_at
              ? formatUTCTime(status.last_success_at, timezone)
              : '还没有成功记录'}
          </div>
        </div>
        <div className="min-w-0">
          <div className="text-xs text-slate-400">最近任务数据量</div>
          <div className="mt-1 flex items-center gap-1.5 font-medium text-slate-700">
            <DatabaseOutlined />
            {status?.processed_rows || 0} 条
          </div>
        </div>
      </div>

      {syncing && status && (
        <div className="border-t border-slate-100 px-5 py-3">
          <div className="mb-1.5 flex flex-wrap justify-between gap-2 text-xs text-slate-500">
            <span>{phaseLabel[status.phase] || '正在处理'}</span>
            <span>
              步骤 {status.completed_steps}/{status.total_steps || '?'}
            </span>
          </div>
          <Progress
            percent={progress}
            showInfo={false}
            size="small"
            status="active"
          />
        </div>
      )}

      {taskError && (
        <div className="border-t border-slate-100 px-4 py-2.5">
          <Alert
            type={status?.status === 'partial' ? 'warning' : 'error'}
            showIcon
            message={
              status?.status === 'partial'
                ? '部分数据同步失败，总汇总表未更新'
                : '同步失败'
            }
            description={taskError}
          />
        </div>
      )}
      {statusError && (
        <div className="border-t border-slate-100 px-4 py-2.5">
          <Alert
            type="warning"
            showIcon
            message="同步状态暂时不可用"
            description={statusError}
          />
        </div>
      )}
      {actionError && (
        <div className="border-t border-slate-100 px-4 py-2.5">
          <Alert
            type="warning"
            showIcon
            message="未能发起同步"
            description={actionError}
          />
        </div>
      )}
    </section>
  )
}
