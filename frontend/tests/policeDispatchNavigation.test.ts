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
  assert.match(analysisSource, /网格核查研判/)
  assert.match(analysisSource, /下发数据复核/)
  assert.match(analysisSource, /<MobileTaskList mode="analysis" \/>/)
  assert.match(mobileTaskSource, /analysisOnly \? 'waiting_analysis'/)
  assert.match(mobileTaskSource, /review_stage: analysisOnly \? 'waiting_analysis'/)
  assert.match(panelSource, /status=pending_review&category=all/)
  assert.match(appSource, /path="\/police-analysis"/)
  assert.match(appSource, /<AnalysisWorkbench \/>/)
})
