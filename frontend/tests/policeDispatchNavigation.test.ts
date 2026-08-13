import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('研判页同时承载无法核实研判和下发数据复核', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/PoliceDispatchWorkbench.tsx', import.meta.url),
    'utf8',
  )
  const analysisSource = readFileSync(
    new URL('../src/pages/AnalysisWorkbench.tsx', import.meta.url),
    'utf8',
  )
  const mobileTaskSource = readFileSync(
    new URL('../src/pages/MobileTaskList.tsx', import.meta.url),
    'utf8',
  )
  const panelSource = readFileSync(
    new URL('../src/components/PoliceDispatchPanel.tsx', import.meta.url),
    'utf8',
  )
  const appSource = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')

  assert.match(pageSource, /mode = 'all'/)
  assert.match(pageSource, /analysisOnly \? 'pending_review'/)
  assert.match(pageSource, /analysisOnly \? 'manual'/)
  assert.match(pageSource, /复核结果仍保存在原下发批次/)
  assert.match(pageSource, /SearchOutlined[\s\S]*from '@ant-design\/icons'/)
  assert.match(analysisSource, /网格核查研判/)
  assert.match(analysisSource, /下发数据复核/)
  assert.match(analysisSource, /<MobileTaskList mode="analysis" \/>/)
  assert.match(mobileTaskSource, /analysisOnly \? 'waiting_analysis'/)
  assert.match(mobileTaskSource, /review_stage:\s*reviewStage/)
  assert.match(mobileTaskSource, /options=\{\[\s*\{ label: '待研判', value: 'waiting_analysis' \},\s*\{ label: '已研判', value: 'analyzed' \}/)
  assert.match(panelSource, /status=pending_review&category=all/)
  assert.match(appSource, /path="\/police-analysis"/)
  assert.match(appSource, /<AnalysisWorkbench \/>/)
})

test('使用搜索图标的下发和小区页面显式导入图标', () => {
  const dispatchSource = readFileSync(
    new URL('../src/pages/PoliceDispatchWorkbench.tsx', import.meta.url),
    'utf8',
  )
  const batchSource = readFileSync(
    new URL('../src/pages/PoliceDispatchBatchDetail.tsx', import.meta.url),
    'utf8',
  )
  const addressSource = readFileSync(
    new URL('../src/pages/PoliceAddressManagement.tsx', import.meta.url),
    'utf8',
  )

  for (const source of [dispatchSource, batchSource, addressSource]) {
    assert.match(source, /SearchOutlined[\s\S]*from '@ant-design\/icons'/)
    assert.match(source, /prefix=\{<SearchOutlined \/>\}/)
  }
})

test('已处理数据可从待发布工作台整批发布且卡片沿用流口任务规范', () => {
  const workbenchSource = readFileSync(
    new URL('../src/pages/PoliceDispatchWorkbench.tsx', import.meta.url),
    'utf8',
  )
  const panelSource = readFileSync(
    new URL('../src/components/PoliceDispatchPanel.tsx', import.meta.url),
    'utf8',
  )
  const batchSource = readFileSync(
    new URL('../src/pages/PoliceDispatchBatchDetail.tsx', import.meta.url),
    'utf8',
  )

  assert.match(workbenchSource, /整批发布（\{publishableCount\}）/)
  assert.match(workbenchSource, /其余.*pending_review.*待复核记录不会发布/s)
  assert.match(workbenchSource, /该条已经审核，无需逐条保存/)
  assert.match(workbenchSource, /className="mobile-task-item-card police-dispatch-task-card"/)
  assert.match(workbenchSource, /mobile-task-item-card__key-row--identity/)
  assert.match(workbenchSource, /mobile-task-item-card__key-row--phone/)
  assert.match(workbenchSource, /mobile-task-item-card__key-row--address/)
  assert.match(workbenchSource, /mobile-task-analysis__label">平台建议/)
  assert.match(workbenchSource, /mobile-task-source-cloud--card/)
  assert.match(workbenchSource, /mobile-task-item-card__footer/)
  assert.doesNotMatch(workbenchSource, /<CopyButton\b/)
  assert.match(workbenchSource, /function CopyIconButton/)
  assert.match(workbenchSource, /<CopyIconButton value=\{value\} label=\{label\} \/>/)
  assert.match(panelSource, /status=pending_publish&category=all/)
  assert.match(panelSource, /确认导入并进入待发布/)
  assert.match(panelSource, /整批发布（\{publishableCount\(item\)\}）/)
  assert.match(batchSource, /整批发布到腾讯（\{publishableCount\}）/)
})
