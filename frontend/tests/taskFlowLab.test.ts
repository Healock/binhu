import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { accessibleNavigationGroups } from '../src/navigation/mobileNavigation.ts'
import {
  defaultTaskFlowPosition,
  mergeTaskFlowInspectors,
  taskFlowLane,
  taskFlowLaneHeight,
  taskFlowLaneNodeId,
  taskFlowNodeId,
} from '../src/utils/taskFlow.ts'

function task(overrides: Record<string, unknown> = {}) {
  return {
    row_key: 'row-1',
    parser_type: '全链条',
    summary: {
      title: '测试对象',
      identity_number: '',
      phone: '',
      source: '',
      address: '',
      current_address: '',
      original_address: '',
      deadline: '',
      date: '',
      result: '',
      analysis: '',
      secondary_feedback: '',
      registration_status: '',
    },
    community: '冬梅社区',
    inspector: '测试人员',
    state: 'unchecked',
    needs_review: false,
    review_stage: '',
    photo_fetched: false,
    source_count: 1,
    conflict: false,
    pending_sync: false,
    sync_state: '',
    priority: 'ordinary',
    watch_marks: [],
    first_dispatch_at: null,
    ...overrides,
  } as any
}

test('任务流按可处理、等待协作和异常分区', () => {
  assert.equal(taskFlowLane(task()), 'ready')
  assert.equal(taskFlowLane(task({ priority: 'waiting_analysis', review_stage: 'waiting_analysis' })), 'waiting')
  assert.equal(taskFlowLane(task({ pending_sync: true })), 'exception')
  assert.equal(taskFlowLane(task({ conflict: true })), 'exception')
  assert.equal(taskFlowNodeId(task()), '全链条:row-1')
})

test('核查人选项跨业务汇总并排除未分配项', () => {
  assert.deepEqual(mergeTaskFlowInspectors([
    [
      { value: '甲', label: '甲', count: 2 },
      { value: '__empty__', label: '待分配', count: 8 },
    ],
    [
      { value: '甲', label: '甲', count: 3 },
      { value: '乙', label: '乙', count: 4 },
    ],
  ]), [
    { value: '甲', label: '甲', count: 5 },
    { value: '乙', label: '乙', count: 4 },
  ])
  assert.deepEqual(defaultTaskFlowPosition('ready', 0), { x: 20, y: 64 })
  assert.deepEqual(defaultTaskFlowPosition('waiting', 1), { x: 20, y: 260 })
  assert.equal(taskFlowLaneNodeId('exception'), 'task-flow-lane-exception')
  assert.equal(taskFlowLaneHeight([
    { lane: 'ready', position: { y: 64 } },
    { lane: 'ready', position: { y: 260 } },
    { lane: 'waiting', position: { y: 64 } },
  ]), 450)
  assert.equal(taskFlowLaneHeight([
    { lane: 'exception', position: { y: 620 } },
  ]), 810)
})

test('任务流内测入口只向超级管理员开放', () => {
  const member = accessibleNavigationGroups('member')
  const admin = accessibleNavigationGroups('admin')
  const superAdmin = accessibleNavigationGroups('super_admin')

  assert.equal(member.some(group => group.items.some(item => item.id === 'task_flow_lab')), false)
  assert.equal(admin.some(group => group.items.some(item => item.id === 'task_flow_lab')), false)
  assert.equal(superAdmin.some(group => group.items.some(item => item.id === 'task_flow_lab')), true)

  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
  assert.match(app, /ProtectedRoute requireRole="super_admin"/)
  assert.match(app, /path="\/task-flow-lab"/)
})

test('任务流聚合现有待办、被动刷新并跳转原任务详情', () => {
  const source = readFileSync(new URL('../src/pages/TaskFlowLab.tsx', import.meta.url), 'utf8')
  const client = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8')
  const workflow = readFileSync(new URL('../src/pages/WorkflowTickets.tsx', import.meta.url), 'utf8')

  assert.match(source, /ReactFlowProvider/)
  assert.match(source, /MOBILE_TASK_TYPES\.map/)
  assert.match(source, /inspectors: \[inspector\]/)
  assert.match(source, /value: 'control'/)
  assert.match(source, /review_stage: 'waiting_analysis'/)
  assert.match(source, /analysisDependencyItem/)
  assert.match(source, /analysisDependencyEdge/)
  assert.match(source, /正在等待基础管控填写研判结果/)
  assert.match(source, /data: \{ system: true, label: '等待研判' \}/)
  assert.match(source, /selectable: false/)
  assert.match(source, /deletable: false/)
  assert.match(source, /workflowApi\.pendingPhotoRequests/)
  assert.match(source, /getPoliceDispatchWorkbench/)
  assert.match(source, /下发数据审核/)
  assert.match(source, /下发数据研判/)
  assert.match(source, /下发数据发布/)
  assert.match(source, /\/photo-tasks\?ticket=/)
  assert.match(source, /AUTO_REFRESH_MS = 30_000/)
  assert.match(source, /refreshTasks\(true\)/)
  assert.match(source, /encodeURIComponent\(task\.parser_type\)/)
  assert.match(source, /encodeURIComponent\(task\.row_key\)/)
  assert.match(source, /writeLayout/)
  assert.match(source, /onMoveEnd=/)
  assert.match(source, /strokeDasharray: '6 5'/)
  assert.match(source, /layoutItems/)
  assert.match(source, /const stacks = new Map/)
  assert.match(source, /MAX_STACK_VISUAL_LAYERS = 5/)
  assert.match(source, /group\.length > 1/)
  assert.match(source, /item\.stackKey = `\$\{item\.lane\}:\$\{item\.category\}`/)
  assert.match(source, /const visualLayer = Math\.min\(index, MAX_STACK_VISUAL_LAYERS\)/)
  assert.match(source, /x: baseX \+ visualLayer \* 10/)
  assert.match(source, /y: baseY - visualLayer \* 12/)
  assert.match(source, /zIndex: 1000 \+ index/)
  assert.match(source, /previous\.has\(item\.id\)/)
  assert.match(source, /<ReactFlow nodes=\{visibleNodes\}/)
  assert.match(source, /Panel as FlowPanel/)
  assert.match(source, /<FlowPanel position="top-left"/)
  assert.match(source, /<FlowPanel position="top-center"/)
  assert.match(source, /processSteps/)
  assert.match(source, /收到任务/)
  assert.match(source, /审核与处理/)
  assert.match(source, /writeLayout/)
  assert.match(source, /onNodeDragStop/)
  assert.match(source, /edge\.data\?\.system !== true/)
  assert.match(source, /protectedIds/)
  assert.match(source, /autoPanOnNodeDrag=\{false\}/)
  assert.match(source, /className=\{`task-flow-node/)
  assert.doesNotMatch(source, /parentId:/)
  assert.doesNotMatch(source, /extent: 'parent'/)
  assert.doesNotMatch(source, /NodeResizer/)
  assert.doesNotMatch(source, /task-flow-lane-/)
  assert.match(client, /const passiveRequest = \{ headers: \{ 'X-User-Activity': '0' \} \}/)
  assert.match(client, /options\.passive \? passiveRequest : activeRequest/)
  assert.match(workflow, /requestedTicketId/)
  assert.match(workflow, /openDetail\(\{ id: requestedTicketId \}\)/)
})
