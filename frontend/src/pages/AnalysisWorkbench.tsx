import { Segmented } from 'antd'
import { useState } from 'react'
import { PageHeader } from '../components/ui'
import { useAuth } from '../context/AuthContext'
import MobileTaskList from './MobileTaskList'
import PoliceDispatchWorkbench from './PoliceDispatchWorkbench'

type AnalysisView = 'flow' | 'dispatch'

export default function AnalysisWorkbench() {
  const { user } = useAuth()
  const canFlow = Boolean(user?.permissions.includes('online.task.manage'))
  const canDispatch = Boolean(user?.permissions.includes('police.dispatch.manage'))
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
      {views.length > 1 && <section className="app-card p-3 sm:p-4">
        <Segmented
          block
          value={view}
          options={views}
          onChange={value => setView(value as AnalysisView)}
        />
      </section>}
      {view === 'flow'
        ? <MobileTaskList mode="analysis" />
        : <PoliceDispatchWorkbench mode="analysis" />}
    </div>
  )
}
