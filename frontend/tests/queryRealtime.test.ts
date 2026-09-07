import assert from 'node:assert/strict'
import test from 'node:test'
import { connectQueryRealtime, querySocketUrl } from '../src/utils/queryRealtime.ts'

class Socket {
  readyState = 1
  sent: string[] = []
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  onerror: (() => void) | null = null
  send(data: string) { this.sent.push(data) }
  close() { this.readyState = 3 }
}

test('socket URL preserves fixed API environment and parser encoding', () => {
  assert.equal(querySocketUrl('/shadow-api/query/live/全链条', 'https://example.test'),
    'wss://example.test/shadow-api/query/live/%E5%85%A8%E9%93%BE%E6%9D%A1')
  assert.throws(() => querySocketUrl('file:///secret', 'https://example.test'))
})

test('edit waits for matching ACK and passes through version conflicts', async () => {
  const socket = new Socket()
  const versions: string[] = []
  const channel = connectQueryRealtime('全链条', v => versions.push(v.data_version), () => {}, {
    url: 'ws://example.test/api/query/live/test', socketFactory: () => socket as any,
  })
  const pending = channel.save(12, { column: '核查结果', value: '离苏', expected_revision: 4 })
  const command = JSON.parse(socket.sent[0])
  socket.onmessage!({ data: JSON.stringify({ type: 'version', data_version: 'v2' }) })
  socket.onmessage!({ data: JSON.stringify({ type: 'error', request_id: command.request_id, status: 409, detail: '版本冲突' }) })
  await assert.rejects(pending, (error: any) => error.response.status === 409)
  assert.deepEqual(versions, ['v2'])
  channel.close()
})

test('disconnect rejects uncertain edits without replay', async () => {
  const socket = new Socket()
  const channel = connectQueryRealtime('全链条', () => {}, () => {}, {
    url: 'ws://example.test/api/query/live/test', socketFactory: () => socket as any,
  })
  const pending = channel.save(1, { column: 'x', value: 'y', expected_revision: 1 })
  socket.onclose!({ code: 1008 })
  await assert.rejects(pending, /待核对/)
  assert.equal(socket.sent.length, 1)
  channel.close()
})

test('unconnected save fails immediately rather than being queued for replay', async () => {
  const socket = new Socket()
  socket.readyState = 0
  const channel = connectQueryRealtime('全链条', () => {}, () => {}, {
    url: 'ws://example.test/api/query/live/test', socketFactory: () => socket as any,
  })
  await assert.rejects(channel.save(1, { column: 'x', value: 'y', expected_revision: 1 }), /连接/)
  assert.equal(socket.sent.length, 0)
  channel.close()
})
