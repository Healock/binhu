import type { ReactNode } from 'react'
import { Progress, Tag } from 'antd'
import { Panel } from './ui'

export interface ExternalDataStat {
  label: string
  value: ReactNode
  hint?: ReactNode
}

interface ExternalDataProgressProps {
  label: ReactNode
  status?: ReactNode
  detail?: ReactNode
  percent?: number
}

interface ExternalDataPanelProps {
  title: string
  description: string
  actions?: ReactNode
  controls?: ReactNode
  stats?: ExternalDataStat[]
  progress?: ExternalDataProgressProps
  children?: ReactNode
  embedded?: boolean
  className?: string
}

function PanelHeader({ title, description, actions }: Pick<ExternalDataPanelProps, 'title' | 'description' | 'actions'>) {
  return (
    <div className="external-data-panel__embedded-header">
      <div className="min-w-0">
        <h2 className="app-card__title">{title}</h2>
        <p className="app-card__description">{description}</p>
      </div>
      {actions && <div className="external-data-panel__actions">{actions}</div>}
    </div>
  )
}

export function ExternalDataStatus({ stats }: { stats: ExternalDataStat[] }) {
  if (!stats.length) return null
  return (
    <div className="external-data-panel__stats">
      {stats.map(item => (
        <div className="external-data-panel__stat" key={item.label}>
          <span className="external-data-panel__stat-label">{item.label}</span>
          <strong className="external-data-panel__stat-value">{item.value}</strong>
          {item.hint && <span className="external-data-panel__stat-hint">{item.hint}</span>}
        </div>
      ))}
    </div>
  )
}

export function ExternalDataProgress({ label, status, detail, percent }: ExternalDataProgressProps) {
  return (
    <div className="external-data-panel__progress">
      <div className="external-data-panel__progress-heading">
        <strong>{label}</strong>
        {status && (typeof status === 'string' ? <Tag color="processing">{status}</Tag> : status)}
      </div>
      {detail && <div className="external-data-panel__progress-detail">{detail}</div>}
      {percent != null && <Progress percent={Math.max(0, Math.min(100, percent))} showInfo={false} size="small" status="active" />}
    </div>
  )
}

export default function ExternalDataPanel({
  title,
  description,
  actions,
  controls,
  stats = [],
  progress,
  children,
  embedded = false,
  className = '',
}: ExternalDataPanelProps) {
  const body = (
    <div className="external-data-panel__content">
      {controls && <div className="external-data-panel__controls">{controls}</div>}
      <ExternalDataStatus stats={stats} />
      {progress && <ExternalDataProgress {...progress} />}
      {children}
    </div>
  )

  if (embedded) {
    return (
      <section className={`external-data-panel external-data-panel--embedded ${className}`.trim()}>
        <PanelHeader title={title} description={description} actions={actions} />
        {body}
      </section>
    )
  }

  return (
    <Panel
      className={`external-data-panel ${className}`.trim()}
      title={title}
      description={description}
      extra={actions && <div className="external-data-panel__actions">{actions}</div>}
    >
      {body}
    </Panel>
  )
}
