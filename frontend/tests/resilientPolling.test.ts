import assert from 'node:assert/strict'
import test from 'node:test'
import { retryDelay, createResilientPoller } from '../src/utils/resilientPolling.ts'

test('retry delay is bounded and jittered around exponential backoff', () => {
  assert.equal(retryDelay(100, 1, 1000, 0, () => 0.5), 100)
  assert.equal(retryDelay(100, 4, 250, 0, () => 0.5), 250)
  assert.equal(retryDelay(100, 2, 1000, 0.25, () => 1), 250)
})

test('poller opens a circuit after bounded failures and recovers after success', async () => {
  const timers: Array<{ callback: () => void; delay: number }> = []
  let attempts = 0
  let circuitOpened = 0
  const poller = createResilientPoller(async () => {
    attempts += 1
    if (attempts < 3) throw new Error('temporary')
  }, {
    intervalMs: 100,
    failureThreshold: 2,
    cooldownMs: 500,
    jitterRatio: 0,
    setTimeout: (callback, delay) => {
      timers.push({ callback, delay })
      return timers.length as unknown as ReturnType<typeof globalThis.setTimeout>
    },
    clearTimeout: () => {},
    onCircuitOpen: () => { circuitOpened += 1 },
  })
  poller.start()
  await Promise.resolve()
  assert.equal(attempts, 1)
  timers.shift()!.callback()
  await Promise.resolve()
  assert.equal(attempts, 2)
  assert.equal(poller.circuitOpen, true)
  assert.equal(circuitOpened, 1)
  assert.equal(timers[0].delay, 500)
  timers.shift()!.callback()
  await Promise.resolve()
  assert.equal(attempts, 3)
  assert.equal(poller.circuitOpen, false)
  poller.stop()
})
