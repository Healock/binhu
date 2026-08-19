import { useEffect, useState } from 'react'
import { Alert, Button, Table, Tag } from 'antd'
import dayjs from 'dayjs'
import {
  confirmVisitSource,
  getVisitSourceStatus,
  previewVisitSource,
} from '../api/client'
import type { VisitSourceRun } from '../types'
import { useAuth } from '../context/AuthContext'
import { Panel } from './ui'


export default function VisitSourcePanel() {
  const { user, systemTimezone } = useAuth()
  const canManage = Boolean(user?.permissions.includes('visit.source.manage'))
  const today = dayjs().format('YYYY-MM-DD')
  const [dates, setDates] = useState<[string, string]>([today, today])
  const [preview, setPreview] = useState<VisitSourceRun[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [status, setStatus] = useState<Record<string, VisitSourceRun>>({})
  const [current, setCurrent] = useState<Record<string, { source_type: string; finished_at: string | null }>>({})
  const [businessDate, setBusinessDate] = useState('')

  const loadStatus = async () => {
    if (!canManage) return
    const value = await getVisitSourceStatus()
    setStatus(value.latest_attempts)
    setCurrent(value.current_sources)
    setBusinessDate(value.business_date)
    const recoverable = Object.values(value.latest_attempts)
      .filter(item => item.status === 'pending_confirmation')
    if (recoverable.length) {
      setPreview(currentPreview => currentPreview.length ? currentPreview : recoverable)
      const first = recoverable[0]
      if (first.start_date && first.end_date) {
        setDates([first.start_date, first.end_date])
      }
    }
    else if (!businessDate) setDates([value.business_date, value.business_date])
  }

  useEffect(() => {
    if (canManage) void loadStatus().catch(() => undefined)
  }, [canManage])

  if (!canManage) return null

  const handlePreview = async () => {
    setLoading(true)
    setError('')
    try {
      const value = await previewVisitSource({
        source: 'both',
        start_date: dates[0],
        end_date: dates[1],
      })
      setPreview(value.data)
    } catch (reason: any) {
      const timeout = reason?.code === 'ECONNABORTED' || reason?.code === 'ETIMEDOUT'
      setError(
        reason?.response?.data?.detail
        || (timeout
          ? '来源读取时间较长，后台可能仍在生成预览，请稍后刷新状态，不要重复点击'
          : '自动获取预览失败，当前数据未替换'),
      )
      await loadStatus().catch(() => undefined)
    } finally {
      setLoading(false)
    }
  }

  const handleConfirm = async (strategy: 'replace' | 'keep') => {
    const runIds = preview.filter(item => item.status === 'pending_confirmation').map(item => item.id)
    if (!runIds.length) return
    setLoading(true)
    setError('')
    try {
      await confirmVisitSource({ run_ids: runIds, strategy })
      setPreview([])
      await loadStatus()
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '自动获取确认失败，当前数据未替换')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Panel
      title="自动获取走访与星级数据"
      description="先只读获取并预览，确认后才替换当前业务日期数据；失败不会覆盖最近成功快照。"
    >
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-sm">
          <span>开始日期</span>
          <input
            type="date"
            value={dates[0]}
            onChange={event => setDates([event.target.value, dates[1]])}
            className="rounded border border-[var(--app-border)] bg-[var(--app-surface)] px-2 py-1"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span>结束日期</span>
          <input
            type="date"
            value={dates[1]}
            onChange={event => setDates([dates[0], event.target.value])}
            className="rounded border border-[var(--app-border)] bg-[var(--app-surface)] px-2 py-1"
          />
        </label>
        <Button type="primary" loading={loading} onClick={() => void handlePreview()}>
          立即获取并预览
        </Button>
      </div>
      {error && <Alert className="mt-3" type="error" showIcon message={error} />}
      {Object.keys(status).length > 0 && (
        <div className="mt-3 space-y-1 text-sm text-[var(--app-text-secondary)]">
          <div>服务器业务日期：{businessDate || '-'} · 时区：{systemTimezone}</div>
          <div>
            当前生效来源：{(['detail', 'rating'] as const).map(kind => {
              const item = current[kind]
              const label = kind === 'detail' ? '走访明细' : '星级评分'
              const source = item?.source_type === 'manual' ? '手动上传' : item ? '自动获取' : '暂无'
              return `${label}·${source}`
            }).join('，')}
          </div>
          <div>最近尝试：{Object.values(status).map(item => `${item.source_page}·${item.status}`).join('，')}</div>
        </div>
      )}
      {preview.length > 0 && (
        <div className="mt-4 space-y-3">
          <Table
            size="small"
            rowKey="id"
            pagination={false}
            dataSource={preview}
            columns={[
              { title: '来源', dataIndex: 'source_page' },
              { title: '状态', dataIndex: 'status', render: value => <Tag color={value === 'pending_confirmation' ? 'warning' : 'error'}>{value}</Tag> },
              { title: '记录数', dataIndex: 'record_count' },
              { title: '有效数', dataIndex: 'valid_count' },
              { title: '问题数', dataIndex: 'issue_count' },
              { title: '新增', render: (_, item) => item.diff?.inserted ?? '-' },
              { title: '变更', render: (_, item) => item.diff?.updated ?? '-' },
              { title: '删除', render: (_, item) => item.diff?.deleted ?? '-' },
              { title: '未匹配/歧义', render: (_, item) => item.diff ? `${item.diff.unmatched}/${item.diff.ambiguous}` : '-' },
              { title: '原因', dataIndex: 'error_message', ellipsis: true },
            ]}
          />
          {preview.some(item => item.status === 'pending_confirmation') && (
            <div className="flex justify-end gap-2">
              <Button loading={loading} onClick={() => void handleConfirm('keep')}>保留旧快照</Button>
              <Button type="primary" loading={loading} onClick={() => void handleConfirm('replace')}>确认替换当前数据</Button>
            </div>
          )}
        </div>
      )}
    </Panel>
  )
}
