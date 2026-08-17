import { Tag, Tooltip } from 'antd'
import type { MobileTaskQmfStatus, QmfFeedbackState } from '../api/client'
import useSystemTime from '../hooks/useSystemTime'

export const QMF_FEEDBACK_OPTIONS: Array<{
  value: QmfFeedbackState
  label: string
}> = [
  { value: 'not_scanned', label: '未扫描' },
  { value: 'stale', label: '待重新核对' },
  { value: 'pending', label: '全民防未核查' },
  { value: 'completed_match', label: '反馈一致' },
  { value: 'completed_mismatch', label: '反馈不一致' },
  { value: 'not_found', label: '全民防无记录' },
  { value: 'error', label: '核对异常' },
]

const STATUS_META: Record<QmfFeedbackState, { label: string; color?: string }> = {
  not_scanned: { label: '未扫描' },
  stale: { label: '待重新核对', color: 'orange' },
  pending: { label: '全民防未核查', color: 'blue' },
  completed_match: { label: '反馈一致', color: 'green' },
  completed_mismatch: { label: '反馈不一致', color: 'red' },
  not_found: { label: '全民防无记录', color: 'gold' },
  error: { label: '核对异常', color: 'orange' },
}

const ERROR_LABELS: Record<string, string> = {
  source_missing: '来源行已不存在',
  source_changed: '来源内容已变化',
  task_not_completed: '任务已不再是完成状态',
  source_not_unique: '任务来源不唯一',
  identity_invalid: '身份证号格式异常',
  result_invalid: '平台核查结果无法识别',
  status_not_configured: '全民防管理端查询未配置',
  status_auth_failed: '全民防管理端认证失败',
  status_auth_unavailable: '全民防管理端认证不可用',
  status_auth_invalid: '全民防管理端认证响应无效',
  status_query_forbidden: '全民防管理端查询无权限',
  unavailable: '全民防管理端暂时不可用',
  ambiguous: '全民防存在多条匹配记录',
  station_mismatch: '全民防记录不属于目标派出所',
  unknown_result: '全民防返回了尚未支持的核查结果',
  unexpected_error: '核对过程出现异常',
}

export function qmfFeedbackLabel(state: QmfFeedbackState) {
  return STATUS_META[state].label
}

export default function QmfFeedbackStatus({
  status,
  compact = false,
}: {
  status: MobileTaskQmfStatus | null | undefined
  compact?: boolean
}) {
  const formatTime = useSystemTime()
  const normalized = status?.state || 'not_scanned'
  const meta = STATUS_META[normalized]
  const origin = status?.origin === 'binhu_automatic'
    ? '滨湖平台自动登记'
    : status?.origin === 'legacy_manual_or_other'
      ? 'APP手工或其他渠道反馈'
      : ''
  const error = status?.error_code
    ? ERROR_LABELS[status.error_code] || '全民防核对未能完成'
    : ''
  const details = [
    `状态：${meta.label}`,
    status?.platform_result ? `平台结果：${status.platform_result}` : '',
    status?.feedback_result ? `全民防结果：${status.feedback_result}` : '',
    status?.checked_at ? `全民防核查时间：${status.checked_at}` : '',
    status?.last_scanned_at ? `平台核对时间：${formatTime(status.last_scanned_at)}` : '',
    origin ? `反馈来源：${origin}` : '',
    error ? `异常原因：${error}` : '',
  ].filter(Boolean)

  return (
    <Tooltip
      title={details.length ? (
        <div className="space-y-1">
          {details.map(detail => <div key={detail}>{detail}</div>)}
        </div>
      ) : '尚未执行全民防反馈核对'}
    >
      <Tag color={meta.color} className={compact ? 'm-0' : undefined}>
        {meta.label}
      </Tag>
    </Tooltip>
  )
}
