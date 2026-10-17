import assert from 'node:assert/strict'
import test from 'node:test'

;(globalThis as any).window = { location: { origin: 'https://example.test' } }
const { appendEventCursor, connectResilientEventSource } = await import('../src/utils/resilientEventSource.ts')

test('SSE reconnect cursor stays on the same endpoint and is encoded', () => {
  assert.equal(
    appendEventCursor('https://example.test/api/events/stream', '123-4'),
    'https://example.test/api/events/stream?last_event_id=123-4',
  )
  assert.equal(appendEventCursor('https://example.test/api/events/stream', ''), 'https://example.test/api/events/stream')
})

test('short-lived successful SSE responses retain backoff and cleanup stops retries', () => {
  const originalWindow = globalThis.window
  const originalDocument = globalThis.document
  const originalSetTimeout = globalThis.setTimeout
  const originalClearTimeout = globalThis.clearTimeout
  const originalNow = Date.now
  const timers = new Map<number, { callback: () => void; delay: number }>()
  const sources: any[] = []
  const delays: number[] = []
  let time = 0
  let id = 0
  let listenerCount = 0
  try {
    ;(globalThis as any).window = { location: { origin: 'https://example.test' },
      addEventListener() { listenerCount += 1 }, removeEventListener() { listenerCount -= 1 } }
    ;(globalThis as any).document = { visibilityState: 'visible',
      addEventListener() { listenerCount += 1 }, removeEventListener() { listenerCount -= 1 } }
    ;(globalThis as any).setTimeout = (callback: () => void, delay: number) => {
      timers.set(++id, { callback, delay })
      return id
    }
    ;(globalThis as any).clearTimeout = (key: number) => timers.delete(key)
    Date.now = () => time
    const connection = connectResilientEventSource('/api/events/stream', () => {}, {
      random: () => 0.5,
      factory: () => {
        const source = { onopen: null, onerror: null, addEventListener() {}, close() {} }
        sources.push(source)
        return source
      },
      onReconnect: event => delays.push(event.delay_ms),
    })
    const closeShortConnection = () => {
      const source = sources.at(-1)
      source.onopen(new Event('open'))
      time += 100
      source.onerror(new Event('error'))
    }
    const reconnect = () => {
      const [key, timer] = [...timers][0]
      timers.delete(key)
      time += timer.delay
      timer.callback()
    }
    for (let attempt = 0; attempt < 6; attempt += 1) {
      if (attempt) reconnect()
      closeShortConnection()
    }
    assert.deepEqual(delays, [1000, 2000, 4000, 8000, 16000, 120000])
    reconnect()
    sources.at(-1).onopen(new Event('open'))
    time += 31_000
    sources.at(-1).onerror(new Event('error'))
    assert.equal(delays.at(-1), 1000)
    connection.close()
    assert.equal(timers.size, 0)
    assert.equal(listenerCount, 0)
  } finally {
    globalThis.window = originalWindow
    globalThis.document = originalDocument
    globalThis.setTimeout = originalSetTimeout
    globalThis.clearTimeout = originalClearTimeout
    Date.now = originalNow
  }
})
