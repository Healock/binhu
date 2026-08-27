import { Tag, Tooltip } from 'antd'
import type { MobileTaskRegistrationLink } from '../api/client'

const STATUS_LABELS: Record<string, { label: string; color: string }> = {
  awaiting_match: { label: '等待登记比对', color: 'blue' },
  matched_once: { label: '已匹配一次', color: 'orange' },
  review_required: { label: '登记待复核', color: 'red' },
  confirmation_pending: { label: '登记同步中', color: 'purple' },
  confirmed: { label: '已登记', color: 'green' },
  cancelled: { label: '登记关联已取消', color: 'default' },
  legacy_completed: { label: '历史待登记', color: 'default' },
}

export default function RegistrationLinkStatus({
  link,
  compact = false,
}: {
  link?: MobileTaskRegistrationLink | null
  compact?: boolean
}) {
  if (!link) return null
  const view = STATUS_LABELS[link.status] || { label: link.status || '待登记', color: 'default' }
  const tag = <Tag color={view.color}>{view.label}</Tag>
  if (compact && !link.reason) return tag
  return link.reason ? <Tooltip title={link.reason}>{tag}</Tooltip> : tag
}
