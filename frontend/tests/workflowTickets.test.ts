import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('基础管控照片工作台支持只读表格、批量领取和 XLSX 导出', () => {
  const pageSource = readFileSync(
    new URL('../src/pages/WorkflowTickets.tsx', import.meta.url),
    'utf8',
  )
  const apiSource = readFileSync(
    new URL('../src/api/client.ts', import.meta.url),
    'utf8',
  )

  assert.match(pageSource, /photo_pending/)
  assert.match(pageSource, /领取全部待领取/)
  assert.match(pageSource, /batchClaimPhotoRequests/)
  assert.match(pageSource, /navigate\('\/data-upload'\)/)
  assert.match(pageSource, /导出 XLSX/)
  assert.match(pageSource, /getCheckboxProps: row => \(\{ disabled: row\.status !== 'queued' \}\)/)
  assert.doesNotMatch(pageSource, /QuerySpreadsheet/)
  assert.match(apiSource, /\/workflow\/photo-requests\/pending/)
  assert.match(apiSource, /\/workflow\/photo-requests\/batch-claim/)
  assert.match(apiSource, /\/workflow\/photo-requests\/pending\/export/)
})
