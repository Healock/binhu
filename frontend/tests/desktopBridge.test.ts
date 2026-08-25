import assert from 'node:assert/strict'
import test from 'node:test'

import { resolveClientUpdateBridge, resolveDesktopBridge } from '../src/desktop/bridge.ts'

test('Android Tauri runtime does not expose the Windows desktop bridge', () => {
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: { userAgent: 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36' },
  })
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: {
      __TAURI__: {
        core: { invoke: async () => undefined },
        event: { listen: async () => () => {} },
      },
    },
  })

  assert.equal(resolveDesktopBridge(), null)
})

test('Android Tauri runtime exposes the common update bridge and Android event', async () => {
  const events: string[] = []
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: { userAgent: 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36' },
  })
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: {
      __TAURI__: {
        core: { invoke: async (command: string) => ({ state: 'idle', command }) },
        event: {
          listen: async (event: string) => {
            events.push(event)
            return () => {}
          },
        },
      },
    },
  })

  const bridge = resolveClientUpdateBridge()
  assert.ok(bridge)
  await bridge.getUpdateStatus()
  bridge.subscribeUpdateState(() => {})
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.deepEqual(events, ['client:update-state'])
})
