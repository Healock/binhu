import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('人工复核进入独立研判页并锁定原批次待审核记录', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/PoliceDispatchWorkbench.tsx', import.meta.url),
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
  assert.match(pageSource, /完成研判后，结果仍回到原下发批次/)
  assert.match(panelSource, /status=pending_review&category=all/)
  assert.match(appSource, /path="\/police-analysis"/)
  assert.match(appSource, /<PoliceDispatchWorkbench mode="analysis" \/>/)
})
