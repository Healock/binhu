import assert from 'node:assert/strict'
import test from 'node:test'

import { downloadBlob } from '../src/utils/fileDownload.ts'

test('桌面客户端通过原生桥接保存 Blob，并保留安全文件名', async () => {
  const calls: Array<{ filename: string; data: number[] }> = []
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: { userAgent: 'Windows' },
  })
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: {
      binhuDesktop: {
        saveFile: async (filename: string, data: number[]) => {
          calls.push({ filename, data })
        },
      },
    },
  })

  await downloadBlob(new Blob([new Uint8Array([1, 2, 3])]), '报告:/2026.xlsx')

  assert.deepEqual(calls, [{
    filename: '报告__2026.xlsx',
    data: [1, 2, 3],
  }])
})

test('网页回退会插入下载链接并延迟释放 Blob URL', async () => {
  let clicked = false
  let revoked = false
  let timeoutCallback: (() => void) | undefined
  const anchor = {
    href: '',
    download: '',
    style: {},
    click: () => { clicked = true },
    remove: () => {},
  }
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: { userAgent: 'Mozilla/5.0' },
  })
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: {
      setTimeout: (callback: () => void) => {
        timeoutCallback = callback
        return 1
      },
    },
  })
  Object.defineProperty(globalThis, 'document', {
    configurable: true,
    value: {
      createElement: () => anchor,
      body: {
        appendChild: () => {},
      },
    },
  })
  Object.defineProperty(globalThis, 'URL', {
    configurable: true,
    value: {
      createObjectURL: () => 'blob:test',
      revokeObjectURL: () => { revoked = true },
    },
  })

  await downloadBlob(new Blob(['xlsx']), '汇总.xlsx')

  assert.equal(clicked, true)
  assert.equal(anchor.download, '汇总.xlsx')
  assert.equal(revoked, false)
  timeoutCallback?.()
  assert.equal(revoked, true)
})
