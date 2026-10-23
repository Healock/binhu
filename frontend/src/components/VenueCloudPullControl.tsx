import { useState } from 'react'
import { Alert, Button, message } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { apiErrorMessage, pullVenueCloudNow } from '../api/client'

export default function VenueCloudPullControl({ onComplete }: { onComplete?: () => void | Promise<void> }) {
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<Awaited<ReturnType<typeof pullVenueCloudNow>> | null>(null)

  const pull = async () => {
    setLoading(true)
    try {
      const response = await pullVenueCloudNow()
      setResult(response)
      if (response.accepted > 0) message.success(`已拉取 ${response.accepted} 条饮酒报备并入库`)
      else if (response.rejected > 0) message.warning(`云端有 ${response.rejected} 条登记被本地校验拒绝，请检查云端状态`)
      else if (response.retry_later > 0) message.warning('云端有登记暂时未入库，将自动重试')
      else if (response.uncertain > 0) message.warning('有登记处于不确定状态，请人工对账')
      else message.info('当前没有可处理的云端登记')
      await onComplete?.()
    } catch (reason: unknown) {
      message.error(apiErrorMessage(reason, '云端拉取失败'))
    } finally {
      setLoading(false)
    }
  }

  return <div className="grid gap-3">
    <div className="flex flex-wrap items-center gap-3">
      <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void pull()}>立即拉取云端队列</Button>
      <span className="text-sm text-[var(--app-text-secondary)]">只读取待处理饮酒报备并写入本地报备库；不会向云端写入业务结果。</span>
    </div>
    {result && <Alert type={result.rejected || result.uncertain ? 'warning' : 'success'} showIcon message={`已入库 ${result.accepted} 条 · 拒绝 ${result.rejected} 条 · 待重试 ${result.retry_later} 条 · 待核对 ${result.uncertain} 条`} />}
  </div>
}
