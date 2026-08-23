import type { ReactNode } from 'react'
import { CloseOutlined } from '@ant-design/icons'

interface FullscreenNoticeDialogProps {
  title: string
  titleId: string
  mark: ReactNode
  subtitle?: ReactNode
  children: ReactNode
  confirmText?: string
  confirmLoading?: boolean
  closeLabel?: string
  onConfirm: () => void
  onClose?: () => void
}

export default function FullscreenNoticeDialog({
  title,
  titleId,
  mark,
  subtitle,
  children,
  confirmText = '知道了',
  confirmLoading = false,
  closeLabel,
  onConfirm,
  onClose,
}: FullscreenNoticeDialogProps) {
  const close = onClose || onConfirm

  return (
    <div className="fullscreen-notice" role="dialog" aria-modal="true" aria-labelledby={titleId}>
      <section className="fullscreen-notice__panel">
        {closeLabel && (
          <button
            type="button"
            className="fullscreen-notice__close"
            aria-label={closeLabel}
            disabled={confirmLoading}
            onClick={close}
          >
            <CloseOutlined />
          </button>
        )}
        <div className="fullscreen-notice__mark">{mark}</div>
        <h2 id={titleId}>{title}</h2>
        {subtitle && <div className="fullscreen-notice__subtitle">{subtitle}</div>}
        <div className="fullscreen-notice__body">{children}</div>
        <button
          type="button"
          className="fullscreen-notice__confirm"
          disabled={confirmLoading}
          onClick={onConfirm}
        >
          {confirmLoading ? '正在确认…' : confirmText}
        </button>
      </section>
    </div>
  )
}
