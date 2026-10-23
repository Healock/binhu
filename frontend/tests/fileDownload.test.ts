import assert from 'node:assert/strict'
import test from 'node:test'

import { downloadBlob, downloadUrl } from '../src/utils/fileDownload.ts'

test('桌面客户端通过原生另存为桥接保存 Blob，并保留安全文件名', async () => {
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
          return true
        },
      },
    },
  })

  const saved = await downloadBlob(new Blob([new Uint8Array([1, 2, 3])]), '报告:/2026.xlsx')

  assert.equal(saved, true)
  assert.deepEqual(calls, [{
    filename: '报告__2026.xlsx',
    data: [1, 2, 3],
  }])
})

test('用户取消桌面另存为时返回未保存且不报错', async () => {
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: { userAgent: 'Windows' },
  })
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: {
      binhuDesktop: {
        saveFile: async () => false,
      },
    },
  })

  assert.equal(await downloadBlob(new Blob(['xlsx']), '汇总.xlsx'), false)
})

test('桌面大文件通过分块桥接保存，避免一次性序列化整个字节数组', async () => {
  const chunks: number[][] = []
  let finished = false
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: { userAgent: 'Windows' },
  })
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: {
      binhuDesktop: {
        saveFile: async () => { throw new Error('legacy bridge should not be used') },
        beginSaveFile: async () => 'handle',
        writeSaveFileChunk: async (_handle: string, data: number[], append: boolean) => {
          assert.equal(append, chunks.length > 0)
          chunks.push(data)
        },
        finishSaveFile: async () => { finished = true },
        cancelSaveFile: async () => { throw new Error('cancel should not be used') },
      },
    },
  })

  const source = new Uint8Array(600 * 1024 + 17)
  source.forEach((_, index) => { source[index] = index % 251 })
  assert.equal(await downloadBlob(new Blob([source]), '登记.zip'), true)
  assert.equal(finished, true)
  assert.equal(chunks.length, 3)
  assert.deepEqual(Uint8Array.from(chunks.flat()), source)
  assert.ok(chunks.every(chunk => chunk.length <= 256 * 1024))
})

test('桌面服务端下载按响应流分块写盘并报告传输进度', async () => {
  const chunks: number[][] = []
  const progress: Array<number | null> = []
  let request: RequestInit | undefined
  const source = new Uint8Array(600 * 1024 + 17)
  source.forEach((_, index) => { source[index] = index % 251 })
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: { userAgent: 'Windows' },
  })
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: {
      location: { pathname: '/' },
      binhuDesktop: {
        saveFile: async () => { throw new Error('legacy bridge should not be used') },
        beginSaveFile: async () => 'stream-handle',
        writeSaveFileChunk: async (_handle: string, data: number[], append: boolean) => {
          assert.equal(append, chunks.length > 0)
          chunks.push(data)
        },
        finishSaveFile: async () => {},
        cancelSaveFile: async () => { throw new Error('cancel should not be used') },
      },
    },
  })
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async (_url: string | URL, init?: RequestInit) => {
      request = init
      return new Response(new ReadableStream({
        start(controller) {
          controller.enqueue(source.subarray(0, 300 * 1024))
          controller.enqueue(source.subarray(300 * 1024))
          controller.close()
        },
      }), { headers: { 'content-length': String(source.length) } })
    },
  })

  assert.equal(await downloadUrl('/api/venue-visits/export-zip', '登记.zip', value => progress.push(value)), 'saved')
  assert.equal(request?.credentials, 'include')
  assert.equal((request?.headers as Record<string, string>)['X-User-Activity'], '1')
  assert.deepEqual(Uint8Array.from(chunks.flat()), source)
  assert.ok(chunks.every(chunk => chunk.length <= 256 * 1024))
  assert.equal(progress.at(-1), 100)
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

  const saved = await downloadBlob(new Blob(['xlsx']), '汇总.xlsx')

  assert.equal(saved, true)
  assert.equal(clicked, true)
  assert.equal(anchor.download, '汇总.xlsx')
  assert.equal(revoked, false)
  timeoutCallback?.()
  assert.equal(revoked, true)
})
