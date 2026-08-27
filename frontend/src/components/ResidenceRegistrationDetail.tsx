import { Alert, Image, Skeleton, Tag } from 'antd'
import type { ResidenceRegistrationDetail as ResidenceDetail } from '../api/client'

const PHOTO_ERRORS: Record<string, string> = {
  photo_invalid_response: '照片响应无法解析',
  photo_business_error: '居住证平台没有返回可用照片',
  photo_too_large: '照片文件过大',
  photo_base64_invalid: '照片编码异常',
  photo_type_invalid: '照片格式不受支持',
  photo_request_error: '照片读取暂时不可用',
}

function value(text: string | null | undefined) {
  return text?.trim() || '暂未提供'
}

export default function ResidenceRegistrationDetail({
  detail,
  loading,
  error,
}: {
  detail: ResidenceDetail | null
  loading: boolean
  error: string
}) {
  if (loading) {
    return <Skeleton active avatar={{ shape: 'square', size: 132 }} paragraph={{ rows: 4 }} />
  }
  if (error) {
    return <Alert type="warning" showIcon message="居住证人员资料暂时无法读取" description={error} />
  }
  if (!detail) return null

  const ageText = typeof detail.age === 'number'
    ? `${detail.age} 岁${detail.birth_date ? `（${detail.birth_date}）` : ''}`
    : value(detail.birth_date)
  const statusColor = detail.registration_status === 'active'
    ? 'green'
    : detail.registration_status === 'cancelled' ? 'default' : 'orange'

  return (
    <div className="grid gap-5 md:grid-cols-[160px_minmax(0,1fr)]">
      <div className="flex min-h-44 items-center justify-center rounded-xl bg-[var(--app-bg-subtle)] p-3">
        {detail.photo_state === 'available' && detail.photo_data_url ? (
          <Image
            src={detail.photo_data_url}
            alt="居住证登记照片"
            className="max-h-52 rounded-lg object-contain"
          />
        ) : (
          <div className="text-center text-sm text-[var(--app-text-secondary)]">
            <div>暂无照片</div>
            {detail.photo_state === 'error' && (
              <div className="mt-1 text-xs">
                {PHOTO_ERRORS[detail.photo_error_code] || '照片读取暂时不可用'}
              </div>
            )}
          </div>
        )}
      </div>
      <dl className="grid min-w-0 gap-x-6 gap-y-4 sm:grid-cols-2">
        <div>
          <dt className="text-xs text-[var(--app-text-secondary)]">注销状态</dt>
          <dd className="mt-1"><Tag color={statusColor}>{detail.registration_status_text}</Tag></dd>
        </div>
        <div>
          <dt className="text-xs text-[var(--app-text-secondary)]">年龄 / 出生日期</dt>
          <dd className="mt-1 break-words text-[var(--app-text-strong)]">{ageText}</dd>
        </div>
        <div>
          <dt className="text-xs text-[var(--app-text-secondary)]">民族</dt>
          <dd className="mt-1 break-words text-[var(--app-text-strong)]">{value(detail.ethnicity)}</dd>
        </div>
        <div>
          <dt className="text-xs text-[var(--app-text-secondary)]">资料更新时间</dt>
          <dd className="mt-1 break-words text-[var(--app-text-strong)]">{value(detail.updated_at)}</dd>
        </div>
        <div className="sm:col-span-2">
          <dt className="text-xs text-[var(--app-text-secondary)]">系统登记住址</dt>
          <dd className="mt-1 break-words text-[var(--app-text-strong)]">{value(detail.registered_address)}</dd>
        </div>
        <div className="sm:col-span-2">
          <dt className="text-xs text-[var(--app-text-secondary)]">户籍地址</dt>
          <dd className="mt-1 break-words text-[var(--app-text-strong)]">{value(detail.household_address)}</dd>
        </div>
      </dl>
    </div>
  )
}
