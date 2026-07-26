import { Alert, Button, Progress, Tag } from 'antd'
import { ClockCircleOutlined, SyncOutlined } from '@ant-design/icons'
import type { SyncStatus } from '../types'
import { formatUTCTime } from '../api/client'

interface Props {
  syncing: boolean
  status: SyncStatus | null
  error: string | null
  onSync: () => void
  timezone?: string
}

const statusLabel = (status?: string) => {
  if (status === 'completed') return { color: 'success', label: '同步完成' }
  if (status === 'failed') return { color: 'error', label: '同步失败' }
  if (status === 'running') return { color: 'processing', label: '正在同步' }
  if (status === 'pending') return { color: 'default', label: '等待处理' }
  return { color: 'default', label: '尚未同步' }
}

export default function SyncPanel({ syncing, status, error, onSync, timezone = 'Asia/Shanghai' }: Props) {
  const progress = status && status.total_rows > 0
    ? Math.round((status.processed_rows / status.total_rows) * 100)
    : 0
  const currentStatus = statusLabel(status?.status)

  return (
    <section className="app-card">
      <div className="app-toolbar justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="app-card__title">数据同步</h2>
            <Tag color={currentStatus.color}>{currentStatus.label}</Tag>
          </div>
          <p className="app-card__description flex items-center gap-1.5">
            <ClockCircleOutlined />
            {status?.finished_at
              ? `最近更新：${formatUTCTime(status.finished_at, timezone)}`
              : '还没有同步记录'}
          </p>
        </div>
        <Button
          type="primary"
          icon={<SyncOutlined spin={syncing} />}
          onClick={onSync}
          loading={syncing}
          disabled={syncing}
        >
          {syncing ? '同步中' : '同步数据'}
        </Button>
      </div>

      {syncing && status && (
        <div className="border-t border-slate-100 px-5 py-3">
          <div className="mb-1.5 flex justify-between text-xs text-slate-500">
            <span>正在处理数据</span>
            <span>{status.processed_rows}/{status.total_rows || '?'}</span>
          </div>
          <Progress
            percent={status.total_rows > 0 ? progress : 50}
            showInfo={false}
            size="small"
            status="active"
          />
        </div>
      )}

      {error && (
        <div className="border-t border-slate-100 p-4">
          <Alert type="error" showIcon message="同步失败" description={error} />
        </div>
      )}
    </section>
  )
}
