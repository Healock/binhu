import test from 'node:test'
import assert from 'node:assert/strict'
import {
  AUDIT_STREAM_SEEN_LIMIT,
  auditStreamUrl,
  clearAuditStreamTimer,
  rememberAuditId,
} from '../src/utils/auditStream.ts'

test('audit stream remembers IDs with bounded de-duplication', () => {
  const seen = new Set<number>()
  assert.equal(rememberAuditId(seen, 12), true)
  assert.equal(rememberAuditId(seen, 12), false)
  for (let id = 13; id <= AUDIT_STREAM_SEEN_LIMIT + 12; id += 1) rememberAuditId(seen, id)
  assert.equal(seen.size, AUDIT_STREAM_SEEN_LIMIT)
  assert.equal(seen.has(12), false)
})

test('audit stream URL carries cursor and action filter', () => {
  assert.equal(
    auditStreamUrl(42, 'user.update', '/api/admin/ops/audit/stream'),
    '/api/admin/ops/audit/stream?after_id=42&action=user.update',
  )
  assert.equal(auditStreamUrl(-1, '', '/stream'), '/stream?after_id=0')
})

test('audit stream cleanup clears timers and is idempotent', () => {
  const cleared: number[] = []
  const clear = (handle: number) => cleared.push(handle)
  assert.equal(clearAuditStreamTimer(9, clear), null)
  assert.deepEqual(cleared, [9])
  assert.equal(clearAuditStreamTimer(null, clear), null)
  assert.deepEqual(cleared, [9])
})
