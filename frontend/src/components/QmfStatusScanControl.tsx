import { useEffect, useState } from 'react'
import { Alert, Button, Modal, Progress, message } from 'antd'
import { getLatestQmfStatusScan, getQmfStatusScan, startQmfStatusScan, type QmfStatusScanRun } from '../api/client'

export default function QmfStatusScanControl() {
  const [run, setRun] = useState<QmfStatusScanRun | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    void getLatestQmfStatusScan().then(value => { if (active) setRun(value) }).catch(() => undefined)
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (!run || !['queued', 'running'].includes(run.status)) return
    let active = true
    const poll = async () => {
      try {
        const value = await getQmfStatusScan(run.id)
        if (active) setRun(value)
      } catch {
        if (active) setError('扫描状态暂时无法读取，任务可能仍在后台运行。')
      }
    }
    const timer = window.setInterval(() => void poll(), 3000)
    return () => { active = false; window.clearInterval(timer) }
  }, [run?.id, run?.status])

  const start = () => Modal.confirm({
    title: '全量核对全民防反馈',
    content: '将冻结当前全部已完成模型三任务清单，并使用四路并发逐条只读核对。扫描不会修改任何业务数据。',
    okText: '开始核对',
    cancelText: '取消',
    onOk: async () => {
      setLoading(true)
      setError('')
      try {
        const value = await startQmfStatusScan()
        setRun(value)
        message.success(value.total_count ? `已加入 ${value.total_count} 条任务` : '当前没有需要扫描的已完成任务')
      } catch (reason: any) {
        const detail = reason?.response?.data?.detail || '全民防反馈扫描启动失败'
        setError(detail)
        message.error(detail)
        throw reason
      } finally {
        setLoading(false)
      }
    },
  })

  const busy = loading || Boolean(run && ['queued', 'running'].includes(run.status))
  const progress = run?.total_count ? Math.round(run.processed_count * 100 / run.total_count) : 0
  return <div className="grid gap-3">
    <div className="flex flex-wrap items-center gap-3">
      <Button type="primary" loading={busy} disabled={busy} onClick={start}>全量核对全民防反馈</Button>
      <span className="text-sm text-[var(--app-text-secondary)]">只读核对已完成模型三任务的外部反馈，不执行登记、上传或回写。</span>
    </div>
    {error && <Alert type="error" showIcon message={error} />}
    {run && <div className="grid gap-2">
      <div className="flex flex-wrap justify-between gap-2 text-sm"><strong>最近任务 #{run.id}</strong><span>{run.status === 'queued' ? '排队中' : run.status === 'running' ? `${run.processed_count}/${run.total_count} 条` : run.status === 'completed' ? '已完成' : run.status === 'partial' ? '部分完成' : '失败'}</span></div>
      <Progress percent={progress} size="small" status={run.status === 'failed' ? 'exception' : run.status === 'completed' ? 'success' : 'active'} />
      {run.status !== 'queued' && run.status !== 'running' && <div className="flex flex-wrap gap-3 text-xs text-[var(--app-text-secondary)]"><span>命中 {run.match_count}</span><span>不一致 {run.mismatch_count}</span><span>待核对 {run.pending_count}</span><span>无记录 {run.not_found_count}</span><span>异常 {run.error_count}</span></div>}
    </div>}
  </div>
}
