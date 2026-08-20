import { useEffect, useState } from 'react'
import { Alert, Button, DatePicker, Table, Tag } from 'antd'
import dayjs from 'dayjs'
import {
  confirmVisitSource,
  getVisitSourceStatus,
  previewVisitSource,
  getExternalAcquisitionRun,
} from '../api/client'
import type { VisitSourceRun } from '../types'
import type { ExternalAcquisitionRun } from '../api/client'
import { useAuth } from '../context/AuthContext'
import ExternalDataPanel from './ExternalDataPanel'

const { RangePicker } = DatePicker


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
  const [job, setJob] = useState<ExternalAcquisitionRun | null>(null)

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
      setJob(value.run)
      const poll = async (runId: number): Promise<void> => {
        const current = await getExternalAcquisitionRun(runId)
        setJob(current)
        if (current.status === 'queued' || current.status === 'running') {
          window.setTimeout(() => void poll(runId), 1500)
          return
        }
        const result = current.result?.data
        if (Array.isArray(result)) setPreview(result)
        await loadStatus()
      }
      void poll(value.run.id)
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

  const currentSources = (['detail', 'rating'] as const).map(kind => {
    const item = current[kind]
    const label = kind === 'detail' ? '走访明细' : '星级评分'
    const source = item?.source_type === 'manual' ? '手动上传' : item ? '自动获取' : '暂无'
    return `${label} · ${source}`
  }).join('，')
  const latestAttempts = Object.values(status).map(item => `${item.source_page} · ${item.status}`).join('，') || '暂无'

  return (
    <ExternalDataPanel
      title="自动获取走访与星级数据"
      description="先只读获取并预览，确认后才替换当前业务日期数据；失败不会覆盖最近成功快照。"
      actions={<Button type="primary" loading={loading} onClick={() => void handlePreview()}>立即获取并预览</Button>}
      controls={<label><span>业务日期范围</span><RangePicker
        value={[dayjs(dates[0]), dayjs(dates[1])]}
        allowClear={false}
        onChange={value => {
          if (value?.[0] && value[1]) setDates([value[0].format('YYYY-MM-DD'), value[1].format('YYYY-MM-DD')])
        }}
      /></label>}
      stats={[
        { label: '服务器业务日期', value: businessDate || '-' },
        { label: '系统时区', value: systemTimezone },
        { label: '当前生效来源', value: currentSources },
        { label: '最近尝试', value: latestAttempts },
      ]}
      progress={job && (job.status === 'queued' || job.status === 'running') ? {
        label: '后台预览任务',
        status: job.status === 'queued' ? '等待执行' : '执行中',
        detail: `${job.message || job.phase}${job.total ? ` · ${job.current}/${job.total}` : ''}`,
        percent: job.progress ?? 0,
      } : undefined}
    >
      {error && <Alert type="error" showIcon message={error} />}
      {preview.length > 0 && (
        <div className="external-data-panel__detail">
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
    </ExternalDataPanel>
  )
}
