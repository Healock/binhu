import { useEffect, useState } from 'react'
import { Alert, Button, DatePicker, Progress, message } from 'antd'
import dayjs, { type Dayjs } from 'dayjs'
import { fetchCodeSummaries, getExternalAcquisitionRun, type ExternalAcquisitionRun } from '../api/client'

const { RangePicker } = DatePicker
const MAX_DAYS = 31

export default function CodeSummaryFetchControl() {
  const today = dayjs().format('YYYY-MM-DD')
  const [range, setRange] = useState<[Dayjs, Dayjs]>([dayjs(today), dayjs(today)])
  const [run, setRun] = useState<ExternalAcquisitionRun | null>(null)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!run || !['queued', 'running'].includes(run.status)) return
    let cancelled = false
    const poll = async () => {
      try {
        const current = await getExternalAcquisitionRun(run.id)
        if (!cancelled) setRun(current)
      } catch {
        if (!cancelled) setError('读取任务状态失败，请稍后刷新。后台任务可能仍在运行。')
      }
    }
    const timer = window.setInterval(() => void poll(), 1500)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [run?.id, run?.status])

  const start = async () => {
    const startDate = range[0].format('YYYY-MM-DD')
    const endDate = range[1].format('YYYY-MM-DD')
    if (dayjs(endDate).diff(dayjs(startDate), 'day') + 1 > MAX_DAYS) {
      setError(`单次最多读取 ${MAX_DAYS} 天，请拆分日期范围`)
      return
    }
    setStarting(true)
    setError('')
    try {
      const result = await fetchCodeSummaries(startDate, endDate)
      setRun(result.run)
      message.success('平安码、管家码读取任务已提交')
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '码数据读取失败，旧快照未改变')
    } finally {
      setStarting(false)
    }
  }

  return <div className="grid gap-3">
    <div className="flex flex-wrap items-end gap-3">
      <label className="grid gap-1 text-sm"><span>业务日期范围</span><RangePicker
        value={range}
        allowClear={false}
        onChange={value => { if (value?.[0] && value[1]) setRange([value[0], value[1]]) }}
      /></label>
      <Button type="primary" loading={starting || Boolean(run && ['queued', 'running'].includes(run.status))} disabled={Boolean(run && ['queued', 'running'].includes(run.status))} onClick={() => void start()}>
        {run && ['queued', 'running'].includes(run.status) ? '读取进行中' : '读取平安码与管家码'}
      </Button>
    </div>
    {error && <Alert type="error" showIcon message={error} />}
    {run && <div className="grid gap-2">
      <div className="flex flex-wrap justify-between gap-2 text-sm"><span>最近读取任务 #{run.id}</span><span>{run.message || run.phase}</span></div>
      <Progress percent={run.progress || 0} size="small" status={run.status === 'failed' ? 'exception' : ['success', 'warning'].includes(run.status) ? 'success' : 'active'} />
    </div>}
  </div>
}
