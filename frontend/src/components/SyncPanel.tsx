import type { SyncStatus } from '../types'
import { formatUTCTime } from '../api/client'

interface Props {
  syncing: boolean
  status: SyncStatus | null
  error: string | null
  onSync: () => void
  timezone?: string
}

export default function SyncPanel({ syncing, status, error, onSync, timezone = 'Asia/Shanghai' }: Props) {
  const progress = status && status.total_rows > 0
    ? Math.round((status.processed_rows / status.total_rows) * 100)
    : 0

  return (
    <div className="bg-white rounded-lg shadow p-4">
      <div className="flex items-center justify-between mb-2">
        <div>
          <h3 className="text-sm font-medium text-gray-700">数据同步</h3>
          {status?.finished_at && (
            <p className="text-xs text-gray-400 mt-0.5">
              数据更新时间: {formatUTCTime(status?.finished_at, timezone)}
            </p>
          )}
        </div>
        <button
          onClick={onSync}
          disabled={syncing}
          className={`px-4 py-1.5 rounded-md text-sm font-medium transition-colors ${
            syncing
              ? 'bg-gray-300 text-gray-500 cursor-not-allowed'
              : 'bg-blue-600 text-white hover:bg-blue-700'
          }`}
        >
          {syncing ? '同步中...' : '🔄 同步数据'}
        </button>
      </div>

      {syncing && status && (
        <div className="mt-2">
          <div className="flex justify-between text-xs text-gray-500 mb-1">
            <span>{status.status === 'running' ? '处理中...' : status.status}</span>
            <span>{status.processed_rows}/{status.total_rows || '?'}</span>
          </div>
          <div className="w-full bg-gray-200 rounded-full h-2">
            <div
              className="bg-blue-500 h-2 rounded-full transition-all duration-500"
              style={{ width: `${status.total_rows > 0 ? progress : 50}%` }}
            />
          </div>
        </div>
      )}

      {error && (
        <div className="mt-2 p-2 bg-red-50 border border-red-200 rounded text-sm text-red-700">
          {error}
        </div>
      )}
    </div>
  )
}
