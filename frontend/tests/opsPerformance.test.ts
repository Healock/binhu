import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

function read(path: string): string {
  return readFileSync(new URL(path, import.meta.url), 'utf8')
}

test('运维中心提供性能、拥堵和后台任务占用面板', () => {
  const page = read('../src/pages/OperationsCenter.tsx')
  const api = read('../src/api/client.ts')

  assert.match(page, /key: 'performance'/)
  assert.match(page, /性能与拥堵/)
  assert.match(page, /P95 响应速度/)
  assert.match(page, /事件循环最大阻塞/)
  assert.match(page, /后台任务占用/)
  assert.match(page, /打开后台任务/)
  assert.match(api, /\/admin\/ops\/performance/)
})

test('拥堵提示提供可执行入口并区分409与5xx', () => {
  const page = read('../src/pages/OperationsCenter.tsx')
  const queue = read('../src/components/AdminTaskQueueFloat.tsx')

  assert.match(page, /signal\.recommended_action/)
  assert.match(page, /onNavigate\(signal\.action_tab\)/)
  assert.match(page, /409 并发冲突单独统计/)
  assert.match(page, /binhu:open-task-queue/)
  assert.match(queue, /binhu:open-task-queue/)
})

test('性能指标说明不宣称后台任务精确CPU归因', () => {
  const help = read('../../backend/help_docs/51-operations-updates.md')
  const agents = read('../../AGENTS.md')

  assert.match(help, /不代表某个任务精确占用了多少 CPU/)
  assert.match(agents, /不能伪装成单任务精确 CPU 归因/)
  assert.match(agents, /409 乐观锁冲突必须单独统计/)
})
