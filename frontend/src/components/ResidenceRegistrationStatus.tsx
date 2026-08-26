import { Tag, Tooltip } from 'antd'
import type { MobileTaskResidenceStatus, ResidenceRegistrationState } from '../api/client'
import useSystemTime from '../hooks/useSystemTime'

const META: Record<ResidenceRegistrationState, { label: string; color?: string }> = {
  pending: { label: '居住证待查询' },
  querying: { label: '居住证查询中', color: 'processing' },
  registered: { label: '居住证已有登记', color: 'blue' },
  first_registration: { label: '首次登记', color: 'green' },
  error: { label: '居住证查询待核对', color: 'orange' },
  stale: { label: '居住证待重新查询', color: 'gold' },
}

const ERROR_LABELS: Record<string, string> = {
  invalid_identity: '身份证号格式异常',
  session_not_ready: '居住证平台尚未登录',
  authentication_expired: '居住证平台登录已失效',
  http_error: '居住证平台返回 HTTP 错误',
  invalid_response: '居住证平台响应无法解析',
  business_error: '居住证平台返回未识别的业务结果',
  request_error: '居住证平台查询暂时不可用',
  source_changed: '任务来源已变化',
  interrupted: '上次查询被服务重启中断，系统将继续查询',
}

export default function ResidenceRegistrationStatus({
  status,
  compact = false,
}: {
  status: MobileTaskResidenceStatus | null | undefined
  compact?: boolean
}) {
  const formatTime = useSystemTime()
  if (!status || (compact && status.state !== 'first_registration')) return null
  const meta = META[status.state] || META.error
  const detail = [
    `居住证状态：${meta.label}`,
    status.checked_at ? `查询时间：${formatTime(status.checked_at)}` : '',
    status.error_code ? `说明：${ERROR_LABELS[status.error_code] || '查询结果需要人工核对'}` : '',
  ].filter(Boolean)
  return (
    <Tooltip title={detail.map(item => <div key={item}>{item}</div>)}>
      <Tag color={meta.color} className={compact ? 'm-0' : undefined}>{meta.label}</Tag>
    </Tooltip>
  )
}
