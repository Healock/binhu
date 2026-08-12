import { Segmented } from 'antd'
import { useState } from 'react'
import { PageHeader } from '../components/ui'
import MobileTaskList from './MobileTaskList'
import PoliceDispatchWorkbench from './PoliceDispatchWorkbench'

type AnalysisView = 'flow' | 'dispatch'

export default function AnalysisWorkbench() {
  const [view, setView] = useState<AnalysisView>('flow')

  return (
    <div className="app-page">
      <PageHeader
        title="研判"
        description="集中处理网格核查中无法核实的数据，以及下发文件中需要人工判断的数据。"
      />
      <section className="app-card p-3 sm:p-4">
        <Segmented
          block
          value={view}
          options={[
            { label: '网格核查研判', value: 'flow' },
            { label: '下发数据复核', value: 'dispatch' },
          ]}
          onChange={value => setView(value as AnalysisView)}
        />
      </section>
      {view === 'flow'
        ? <MobileTaskList mode="analysis" />
        : <PoliceDispatchWorkbench mode="analysis" />}
    </div>
  )
}
