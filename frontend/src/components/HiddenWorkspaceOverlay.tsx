import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import { CloseOutlined, LockOutlined } from '@ant-design/icons'

interface HiddenWorkspaceOverlayProps {
  open: boolean
  onClose: () => void
}

/**
 * Deliberately stays outside the router: this workspace is an in-page easter
 * egg, so opening it must not change the address or the user's navigation
 * history.
 */
export default function HiddenWorkspaceOverlay({ open, onClose }: HiddenWorkspaceOverlayProps) {
  useEffect(() => {
    if (!open) return undefined
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', handleKeyDown)
    }
  }, [onClose, open])

  if (!open) return null

  return createPortal(
    <div
      className="hidden-workspace-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="hidden-workspace-title"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <section className="hidden-workspace-overlay__panel">
        <button
          type="button"
          className="hidden-workspace-overlay__close"
          aria-label="关闭隐藏页面"
          title="关闭"
          onClick={onClose}
        >
          <CloseOutlined />
        </button>
        <div className="hidden-workspace-overlay__icon" aria-hidden="true">
          <LockOutlined />
        </div>
        <p className="hidden-workspace-overlay__eyebrow">隐藏工作区</p>
        <h2 id="hidden-workspace-title">这里将承载新的工作台</h2>
        <p className="hidden-workspace-overlay__description">
          页面入口已经准备好。后续确定具体用途后，可以在这里加入独立功能，不会影响现有页面地址和导航历史。
        </p>
        <button type="button" className="hidden-workspace-overlay__action" onClick={onClose}>
          返回当前页面
        </button>
      </section>
    </div>,
    document.body,
  )
}
