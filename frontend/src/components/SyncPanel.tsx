import { useEffect, useMemo, useState } from 'react'
import { Alert, Button, Tag, Tooltip } from 'antd'
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
import ExternalDataPanel from './ExternalDataPanel'

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
    <ExternalDataPanel
      title="数据同步"
      description={syncing
        ? status?.current_item || phaseLabel[status?.phase || 'queued']
        : schedule?.enabled
          ? `自动同步每 ${intervalLabel(schedule.interval_minutes)}执行一次`
          : '自动同步已关闭'}
      actions={(
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
      )}
      stats={[
        {
          label: '当前状态',
          value: <span className="flex flex-wrap items-center gap-2"><Tag color={currentStatus.color}>{currentStatus.label}</Tag>{status?.task_id ? <Tag>{sourceLabel}</Tag> : null}</span>,
        },
        {
          label: '下一次自动同步',
          value: <span className="flex items-center gap-1.5"><ClockCircleOutlined />{schedule?.enabled && countdown ? countdown : '已关闭'}</span>,
          hint: schedule?.next_run_at && schedule.enabled ? formatUTCTime(schedule.next_run_at, timezone) : undefined,
        },
        {
          label: '最近成功',
          value: status?.last_success_at ? formatUTCTime(status.last_success_at, timezone) : '还没有成功记录',
        },
        {
          label: '最近任务数据量',
          value: <span className="flex items-center gap-1.5"><DatabaseOutlined />{status?.processed_rows || 0} 条</span>,
        },
      ]}
      progress={syncing && status ? {
        label: phaseLabel[status.phase] || '正在处理',
        status: `步骤 ${status.completed_steps}/${status.total_steps || '?'}`,
        detail: status.current_item,
        percent: progress,
      } : undefined}
    >

      {taskError && (
        <Alert
          type={status?.status === 'partial' ? 'warning' : 'error'}
          showIcon
          message={status?.status === 'partial' ? '部分数据同步失败，总汇总表未更新' : '同步失败'}
          description={taskError}
        />
      )}
      {statusError && (
        <Alert type="warning" showIcon message="同步状态暂时不可用" description={statusError} />
      )}
      {actionError && (
        <Alert type="warning" showIcon message="未能发起同步" description={actionError} />
      )}
    </ExternalDataPanel>
  )
}
