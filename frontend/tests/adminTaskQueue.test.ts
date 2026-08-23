import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

function read(path: string): string {
  return readFileSync(new URL(path, import.meta.url), 'utf8')
}

test('管理员任务队列使用真实只读接口和被动轮询', () => {
  const api = read('../src/api/client.ts')
  const component = read('../src/components/AdminTaskQueueFloat.tsx')
  const layout = read('../src/components/Layout.tsx')

  assert.match(api, /api\.get\('\/admin\/task-queue', passiveRequest\)/)
  assert.match(component, /CLOSED_REFRESH_MS = 30_000/)
  assert.match(component, /OPEN_REFRESH_MS = 10_000/)
  assert.match(component, /document\.visibilityState !== 'visible'/)
  assert.match(component, /<FloatButton/)
  assert.match(component, /<Drawer/)
  assert.match(layout, /<AdminTaskQueueFloat \/>/)
})

test('任务队列只向管理员账号展示并且不声明敏感业务字段', () => {
  const api = read('../src/api/client.ts')
  const component = read('../src/components/AdminTaskQueueFloat.tsx')
  const typeBlock = api.slice(
    api.indexOf('export interface AdminTaskQueueItem'),
    api.indexOf('export interface AdminTaskQueueResponse'),
  )

  assert.match(component, /\['admin', 'super_admin'\]\.includes\(code\)/)
  assert.match(component, /\['admin', 'super_admin'\]\.includes\(user\.role\)/)
  for (const forbidden of ['payload', 'identity_number', 'phone', 'address', 'error_message']) {
    assert.doesNotMatch(typeBlock, new RegExp(forbidden))
  }
})

test('侧栏版本区只显示版本号', () => {
  const layout = read('../src/components/Layout.tsx')
  assert.match(layout, />v\{clientVersion\}<\/div>/)
  assert.doesNotMatch(layout, /数据管理中心 · v\{clientVersion\}/)
})
