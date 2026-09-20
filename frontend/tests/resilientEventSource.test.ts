import assert from 'node:assert/strict'
import test from 'node:test'

;(globalThis as any).window = { location: { origin: 'https://example.test' } }
const { appendEventCursor } = await import('../src/utils/resilientEventSource.ts')

test('SSE reconnect cursor stays on the same endpoint and is encoded', () => {
  assert.equal(
    appendEventCursor('https://example.test/api/events/stream', '123-4'),
    'https://example.test/api/events/stream?last_event_id=123-4',
  )
  assert.equal(appendEventCursor('https://example.test/api/events/stream', ''), 'https://example.test/api/events/stream')
})
