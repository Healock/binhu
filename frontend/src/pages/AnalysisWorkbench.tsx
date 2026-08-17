import { useCallback, useState } from 'react'
import { PageHeader } from '../components/ui'
import { useAuth } from '../context/AuthContext'
import MobileTaskList from './MobileTaskList'
import PoliceDispatchWorkbench from './PoliceDispatchWorkbench'

type AnalysisView = 'flow' | 'dispatch'

export default function AnalysisWorkbench() {
  const { user } = useAuth()
  const canFlow = Boolean(user?.permissions.includes('online.task.manage'))
  const canDispatch = Boolean(user?.permissions.includes('police.dispatch.manage'))
  const [flowCount, setFlowCount] = useState<number | null>(null)
  const [dispatchCount, setDispatchCount] = useState<number | null>(null)
  const reportFlowCount = useCallback((count: number) => setFlowCount(count), [])
  const reportDispatchCount = useCallback((count: number) => setDispatchCount(count), [])
  const views = [
    ...(canFlow ? [{ label: '已下发数据研判', value: 'flow' as const }] : []),
    ...(canDispatch ? [{ label: '未下发数据研判', value: 'dispatch' as const }] : []),
  ]
  const [requestedView, setView] = useState<AnalysisView>(canFlow ? 'flow' : 'dispatch')
  const view = views.some(option => option.value === requestedView)
    ? requestedView
    : views[0]?.value || 'flow'

  return (
    <div className="app-page">
      <PageHeader
        title="研判"
        description="集中处理已下发数据中的研判事项，以及尚未下发数据的人工判断事项。"
      />
      {views.length > 0 && <section className="analysis-workbench-switch app-card p-3 sm:p-4">
        <div className="mobile-task-priority-grid analysis-workbench-switch__grid" aria-label="研判类型">
          {views.map(option => (
            <button
              key={option.value}
              type="button"
              className={`mobile-task-priority-card${view === option.value ? ' is-active' : ''}`}
              aria-pressed={view === option.value}
              onClick={() => setView(option.value)}
            >
              <span>{option.label}</span>
              <strong>{option.value === 'flow' ? flowCount ?? '—' : dispatchCount ?? '—'}</strong>
            </button>
          ))}
        </div>
      </section>}
      {canFlow && <div hidden={view !== 'flow'}>
        <MobileTaskList mode="analysis" manageUrl={view === 'flow'} onAnalysisCountChange={reportFlowCount} />
      </div>}
      {canDispatch && <div hidden={view !== 'dispatch'}>
        <PoliceDispatchWorkbench mode="analysis" manageUrl={view === 'dispatch'} onAnalysisCountChange={reportDispatchCount} />
      </div>}
    </div>
  )
}
