import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('工单和系统设置页面使用明确的间距布局', () => {
  const workflowSource = readFileSync(
    new URL('../src/pages/WorkflowTickets.tsx', import.meta.url),
    'utf8',
  )
  const settingsSource = readFileSync(
    new URL('../src/pages/SystemSettings.tsx', import.meta.url),
    'utf8',
  )
  const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')

  assert.match(workflowSource, /workflow-photo-toolbar__filters/)
  assert.match(workflowSource, /workflow-photo-toolbar__actions/)
  assert.match(workflowSource, /workflow-ticket-detail__section/)
  assert.match(styles, /\.workflow-ticket-detail\s*\{[^}]*gap:\s*24px/s)
  assert.match(styles, /\.workflow-photo-toolbar__filters,[\s\S]*?gap:\s*12px/)

  assert.match(settingsSource, /settings-field--counted/)
  assert.match(settingsSource, /settings-field__hint/)
  assert.match(styles, /\.settings-field--counted\s*\{[^}]*padding-bottom:\s*22px/s)
})
