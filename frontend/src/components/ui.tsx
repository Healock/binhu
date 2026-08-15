import type { ReactNode } from 'react'
import { Alert, Empty, Spin } from 'antd'

interface PageHeaderProps {
  title: string
  description?: string
  actions?: ReactNode
}

export function PageHeader({ title, description, actions }: PageHeaderProps) {
  return (
    <header className="page-header">
      <div>
        <h1 className="page-header__title">{title}</h1>
        {description && <p className="page-header__description">{description}</p>}
      </div>
      {actions && <div className="page-header__actions">{actions}</div>}
    </header>
  )
}

interface PanelProps {
  title?: string
  description?: string
  extra?: ReactNode
  children: ReactNode
  padded?: boolean
  className?: string
}

export function Panel({ title, description, extra, children, padded = true, className = '' }: PanelProps) {
  const hasHeader = title || description || extra
  return (
    <section className={`app-card ${className}`.trim()}>
      {hasHeader && (
        <div className="app-card__header">
          <div>
            {title && <h2 className="app-card__title">{title}</h2>}
            {description && <p className="app-card__description">{description}</p>}
          </div>
          {extra}
        </div>
      )}
      <div className={padded ? 'app-card__body' : ''}>{children}</div>
    </section>
  )
}

export function LoadingState({ label = '加载中...' }: { label?: string }) {
  return (
    <div className="app-empty" role="status">
      <Spin size="small" />
      <span className="ml-3">{label}</span>
    </div>
  )
}

export function EmptyState({ label = '暂无数据' }: { label?: string }) {
  return (
    <div className="app-empty">
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={label} />
    </div>
  )
}

interface FeedbackProps {
  message: string
  success?: boolean
  className?: string
}

interface ListToolbarProps {
  filters?: ReactNode
  meta?: ReactNode
  actions?: ReactNode
  notice?: ReactNode
  extra?: ReactNode
  className?: string
}

export function ListToolbar({ filters, meta, actions, notice, extra, className = '' }: ListToolbarProps) {
  return (
    <section className={`app-card list-toolbar ${className}`.trim()}>
      {notice && <div className="list-toolbar__notice">{notice}</div>}
      {filters && <div className="list-toolbar__filters">{filters}</div>}
      {extra && <div className="list-toolbar__extra">{extra}</div>}
      {(meta || actions) && (
        <div className="list-toolbar__status-row">
          <div className="list-toolbar__meta">{meta}</div>
          <div className="list-toolbar__actions">{actions}</div>
        </div>
      )}
    </section>
  )
}

export function Feedback({ message, success = false, className = '' }: FeedbackProps) {
  if (!message) return null
  return (
    <Alert
      className={`app-feedback ${className}`.trim()}
      type={success ? 'success' : 'error'}
      showIcon
      message={message}
    />
  )
}
