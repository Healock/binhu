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

test('任务队列按需加载问题明细并只对白名单写回提供安全重试', () => {
  const api = read('../src/api/client.ts')
  const component = read('../src/components/AdminTaskQueueFloat.tsx')

  assert.match(api, /getAdminTaskQueueDetails/)
  assert.match(api, /retryAdminPhotoWriteback/)
  assert.match(component, /查看问题明细/)
  assert.match(component, /原因分析/)
  assert.match(component, /建议处理/)
  assert.match(component, /已禁止盲目重试/)
  assert.match(component, /detail\.retry_kind !== 'photo_outbox'/)
})

test('侧栏滚动条隐藏但滚动容器保持可用', () => {
  const layout = read('../src/components/Layout.tsx')
  const styles = read('../src/index.css')

  assert.match(layout, /app-sidebar__nav flex-1 overflow-y-auto/)
  assert.match(styles, /\.app-sidebar__nav\s*\{[^}]*scrollbar-width:\s*none/s)
  assert.match(styles, /\.app-sidebar__nav::\-webkit-scrollbar/)
})
