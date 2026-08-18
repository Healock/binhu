import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('Rete 工作流原型使用流程节点和自动编排并保持超级管理员隔离', () => {
  const page = readFileSync(new URL('../src/pages/ReteTaskFlowLab.tsx', import.meta.url), 'utf8')
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
  const oldLab = readFileSync(new URL('../src/pages/TaskFlowLab.tsx', import.meta.url), 'utf8')
  const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'))

  assert.equal(pkg.dependencies.rete.startsWith('^2.'), true)
  assert.equal(pkg.dependencies['rete-react-plugin'].startsWith('^2.'), true)
  assert.equal(pkg.dependencies['rete-auto-arrange-plugin'].startsWith('^2.'), true)
  assert.match(page, /AutoArrangePlugin/)
  assert.match(page, /'elk\.algorithm': 'layered'/)
  assert.match(page, /FLOW_NODES/)
  assert.match(page, /新下发数据/)
  assert.match(page, /网格员核查/)
  assert.match(page, /基础管控研判/)
  assert.match(page, /调取照片/)
  assert.match(page, /完成与归档/)
  assert.match(page, /getPoliceDispatchWorkbench/)
  assert.match(page, /workflowApi\.pendingPhotoRequests/)
  assert.match(page, /listMobileTasks/)
  assert.match(page, /进入队列/)
  assert.match(app, /path="\/task-flow-rete-lab"/)
  assert.match(app, /ProtectedRoute requireRole="super_admin"/)
  assert.match(oldLab, /试用 Rete\.js 工作流/)
})
