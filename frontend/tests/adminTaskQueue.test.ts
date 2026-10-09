import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { clampFloatingActionPosition } from '../src/utils/floatingActionPosition.ts'

function read(path: string): string {
  return readFileSync(new URL(path, import.meta.url), 'utf8')
}

test('悬浮球保留可见边界和手机底部导航空间', () => {
  assert.deepEqual(clampFloatingActionPosition({ x: -100, y: -100 }, 390, 844, 76), { x: 12, y: 72 })
  assert.deepEqual(clampFloatingActionPosition({ x: 1800, y: 900 }, 390, 844, 76), { x: 326, y: 716 })
  assert.deepEqual(clampFloatingActionPosition({ x: 500, y: 200 }, 1280, 720, 12), { x: 500, y: 200 })
})

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
  assert.match(layout, /<FloatingActionMenu>/)
  assert.match(layout, /<AdminTaskQueueFloat \/>/)
  assert.match(layout, /<MyTaskHistoryFloat \/>/)
})

test('后台队列和我的任务记录使用统一速度拨盘，默认不再重叠', () => {
  const menu = read('../src/components/FloatingActionMenu.tsx')
  const queue = read('../src/components/AdminTaskQueueFloat.tsx')
  const history = read('../src/components/MyTaskHistoryFloat.tsx')
  const styles = read('../src/index.css')

  assert.match(menu, /app-speed-dial__main/)
  assert.match(menu, /打开快捷功能/)
  assert.match(menu, /收起快捷功能/)
  assert.match(queue, /admin-task-queue-float.*is-speed-dial-visible/)
  assert.match(history, /my-task-history-float.*is-speed-dial-visible/)
  assert.match(styles, /\.app-speed-dial__main[\s\S]*z-index: 40/)
  assert.match(styles, /\.admin-task-queue-float,[\s\S]*\.my-task-history-float[\s\S]*pointer-events: none/)
  assert.match(styles, /\.my-task-history-float \{[\s\S]*left: var\(--action-x\)/)
  for (const component of [menu, queue, history]) assert.doesNotMatch(component, /tooltip=/)
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

test('任务队列按需加载问题明细并禁止对历史外部队列盲目重试', () => {
  const api = read('../src/api/client.ts')
  const component = read('../src/components/AdminTaskQueueFloat.tsx')

  assert.match(api, /getAdminTaskQueueDetails/)
  assert.doesNotMatch(api, /retryAdminPhotoWriteback/)
  assert.match(component, /查看问题明细/)
  assert.match(component, /原因分析/)
  assert.match(component, /建议处理/)
  assert.match(component, /请按建议处理/)
  assert.doesNotMatch(component, /photo_outbox|写回队列/)
})

test('侧栏滚动条隐藏但滚动容器保持可用', () => {
  const layout = read('../src/components/Layout.tsx')
  const styles = read('../src/index.css')

  assert.match(layout, /app-sidebar__nav flex-1 overflow-y-auto/)
  assert.match(styles, /\.app-sidebar__nav\s*\{[^}]*scrollbar-width:\s*none/s)
  assert.match(styles, /\.app-sidebar__nav::\-webkit-scrollbar/)
})
