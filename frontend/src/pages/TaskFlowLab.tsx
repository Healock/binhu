import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  NodeToolbar,
  Panel as FlowPanel,
  Position,
  ReactFlow,
  ReactFlowProvider,
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  useReactFlow,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
  type NodeProps,
  type Viewport,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { CompressOutlined, EnterOutlined, ExperimentOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons'
import { Alert, Button, Empty, Input, Segmented, Select, Skeleton, Tag, message } from 'antd'
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  getMobileTaskFilterOptions,
  getPoliceDispatchWorkbench,
  listMobileTasks,
  workflowApi,
  type MobileTaskFilterOption,
  type MobileTaskItem,
  type PendingPhotoRequest,
  type PoliceDispatchBatch,
} from '../api/client'
import useDebouncedValue from '../hooks/useDebouncedValue'
import useMobileViewport from '../hooks/useMobileViewport'
import { MOBILE_TASK_TYPES } from '../utils/mobileTaskRouting'
import { TASK_FLOW_LANES, taskFlowLane, taskFlowNodeId, type TaskFlowLane } from '../utils/taskFlow'

const AUTO_REFRESH_MS = 30_000
const MAX_PERSON_TASKS_PER_TYPE = 100
const MAX_CONTROL_ANALYSIS_NODES = 60
const MAX_CONTROL_PHOTO_NODES = 50
const LAST_INSPECTOR_KEY = 'binhu-task-flow-lab:last-inspector'
const LAST_VIEW_KEY = 'binhu-task-flow-lab:last-view'
const LAYOUT_KEY_PREFIX = 'binhu-task-flow-lab:layout-v6:'

type TaskFlowView = 'person' | 'control'
type LayoutDensity = 'compact' | 'standard' | 'comfortable'

const DENSITY_LAYOUTS: Record<LayoutDensity, { xGap: number; yGap: number; zoom: number }> = {
  compact: { xGap: 330, yGap: 190, zoom: 0.76 },
  standard: { xGap: 380, yGap: 220, zoom: 0.7 },
  comfortable: { xGap: 460, yGap: 260, zoom: 0.64 },
}

interface TaskFlowItem {
  id: string
  lane: TaskFlowLane
  category: string
  title: string
  statusLabel: string
  statusColor: string
  community: string
  deadline: string
  description: string
  owner: string
  openPath: string
  weight: number
}

type TaskFlowNodeData = {
  item: TaskFlowItem
  fresh: boolean
  toolbarVisible: boolean
  openTask: (path: string) => void
} & Record<string, unknown>

type TaskNode = Node<TaskFlowNodeData, 'task'>

interface SavedTaskFlowLayout {
  positions: Record<string, { x: number; y: number }>
  edges: Edge[]
  viewport: Viewport | null
  density: LayoutDensity
}

interface LoadedTaskFlowItems {
  items: TaskFlowItem[]
  total: number
  loadedTotal: number
}

function stateMeta(task: MobileTaskItem) {
  if (task.state === 'completed') return { label: '已完成', color: 'green' }
  if (task.state === 'checked') return { label: '待补结果', color: 'orange' }
  return { label: '未核查', color: 'gold' }
}

function personTaskItem(task: MobileTaskItem): TaskFlowItem {
  const state = stateMeta(task)
  const lane = taskFlowLane(task)
  const waitingForAnalysis = lane === 'waiting'
    && (task.priority === 'waiting_analysis' || task.review_stage === 'waiting_analysis')
  return {
    id: taskFlowNodeId(task), lane, category: task.parser_type,
    title: task.summary.title || '未填写姓名',
    statusLabel: waitingForAnalysis ? '等待研判' : state.label,
    statusColor: waitingForAnalysis ? 'purple' : state.color,
    community: task.community, deadline: task.summary.deadline,
    description: task.summary.address, owner: task.inspector || '待分配',
    openPath: `/tasks/${encodeURIComponent(task.parser_type)}/${encodeURIComponent(task.row_key)}?scope=all`,
    weight: 1,
  }
}

function analysisTaskItem(task: MobileTaskItem): TaskFlowItem {
  return {
    id: `analysis:${taskFlowNodeId(task)}`, lane: 'ready', category: '网格核查研判',
    title: task.summary.title || '未填写姓名', statusLabel: '待研判', statusColor: 'purple',
    community: task.community, deadline: task.summary.deadline,
    description: task.summary.address || task.parser_type, owner: task.inspector || '核查人未填写',
    openPath: `/police-analysis/${encodeURIComponent(task.parser_type)}/${encodeURIComponent(task.row_key)}?scope=all`,
    weight: 1,
  }
}

function photoTaskItem(task: PendingPhotoRequest): TaskFlowItem {
  return {
    id: `photo:${task.id}`, lane: task.overdue ? 'exception' : 'ready', category: '调照片',
    title: task.subject_name || task.title || task.ticket_no,
    statusLabel: task.overdue ? '已逾期' : '待领取', statusColor: task.overdue ? 'red' : 'blue',
    community: task.community_name, deadline: '', description: task.source_label || task.ticket_no,
    owner: task.requester_name || '申请人未匹配', openPath: `/photo-tasks?ticket=${task.id}`, weight: 1,
  }
}

function dispatchBatchItems(batch: PoliceDispatchBatch): TaskFlowItem[] {
  const result: TaskFlowItem[] = []
  const regularReview = Math.max(0, batch.counts.pending_review - batch.counts.abnormal)
  if (regularReview) result.push({
    id: `dispatch:${batch.id}:review`, lane: 'ready', category: '下发数据审核',
    title: `批次 #${batch.id} · ${batch.file_name}`, statusLabel: `待审核 ${regularReview}`, statusColor: 'orange',
    community: batch.sheet_name, deadline: '', description: '核对社区、登记情况、地址和下发去向',
    owner: batch.imported_by || '基础管控', openPath: `/police-tasks?batch=${batch.id}&status=pending_review&category=all`, weight: regularReview,
  })
  if (batch.counts.abnormal) result.push({
    id: `dispatch:${batch.id}:analysis`, lane: 'ready', category: '下发数据研判',
    title: `批次 #${batch.id} · ${batch.file_name}`, statusLabel: `待研判 ${batch.counts.abnormal}`, statusColor: 'purple',
    community: batch.sheet_name, deadline: '', description: '处理导入后无法直接确定去向的数据',
    owner: batch.imported_by || '基础管控', openPath: `/police-tasks?batch=${batch.id}&status=pending_review&category=manual`, weight: batch.counts.abnormal,
  })
  if (batch.counts.pending_publish) result.push({
    id: `dispatch:${batch.id}:publish`, lane: 'ready', category: '下发数据发布',
    title: `批次 #${batch.id} · ${batch.file_name}`, statusLabel: `待发布 ${batch.counts.pending_publish}`, statusColor: 'blue',
    community: batch.sheet_name, deadline: '', description: '审核完成，等待选择任务并发布到腾讯全链条',
    owner: batch.imported_by || '基础管控', openPath: `/police-tasks?batch=${batch.id}&status=pending_publish&category=all`, weight: batch.counts.pending_publish,
  })
  const exceptionCount = batch.counts.conflict + batch.counts.needs_reconciliation + batch.counts.retryable
  if (exceptionCount) {
    const status = batch.counts.conflict ? 'conflict' : batch.counts.needs_reconciliation ? 'needs_reconciliation' : 'retryable'
    result.push({
      id: `dispatch:${batch.id}:exception`, lane: 'exception', category: '下发数据异常',
      title: `批次 #${batch.id} · ${batch.file_name}`, statusLabel: `需处理 ${exceptionCount}`, statusColor: 'red',
      community: batch.sheet_name, deadline: '',
      description: `冲突 ${batch.counts.conflict} · 待对账 ${batch.counts.needs_reconciliation} · 可重试 ${batch.counts.retryable}`,
      owner: '基础管控', openPath: `/police-tasks?batch=${batch.id}&status=${status}&category=all`, weight: exceptionCount,
    })
  }
  return result
}

function layoutItems(items: TaskFlowItem[], density: LayoutDensity, previous: Map<string, { x: number; y: number }>) {
  const layout = DENSITY_LAYOUTS[density]
  const laneIndex: Record<TaskFlowLane, number> = { ready: 0, waiting: 0, exception: 0 }
  return items.map(item => {
    if (previous.has(item.id)) return { item, position: previous.get(item.id)! }
    const index = laneIndex[item.lane]++
    const lane = item.lane === 'ready' ? 0 : item.lane === 'waiting' ? 1 : 2
    return {
      item,
      position: {
        x: 40 + lane * (layout.xGap * 2.2),
        y: 80 + Math.floor(index / 2) * layout.yGap,
      },
    }
  })
}

function readLayout(contextKey: string): SavedTaskFlowLayout {
  try {
    const parsed = JSON.parse(localStorage.getItem(`${LAYOUT_KEY_PREFIX}${encodeURIComponent(contextKey)}`) || '{}')
    return {
      positions: parsed?.positions && typeof parsed.positions === 'object' ? parsed.positions : {},
      edges: Array.isArray(parsed?.edges) ? parsed.edges : [],
      viewport: parsed?.viewport && Number.isFinite(parsed.viewport.x) && Number.isFinite(parsed.viewport.y) && Number.isFinite(parsed.viewport.zoom) ? parsed.viewport : null,
      density: ['compact', 'standard', 'comfortable'].includes(parsed?.density) ? parsed.density : 'standard',
    }
  } catch {
    return { positions: {}, edges: [], viewport: null, density: 'standard' }
  }
}

function writeLayout(contextKey: string, nodes: TaskNode[], edges: Edge[], viewport: Viewport | null, density: LayoutDensity) {
  if (!contextKey) return
  localStorage.setItem(`${LAYOUT_KEY_PREFIX}${encodeURIComponent(contextKey)}`, JSON.stringify({
    positions: Object.fromEntries(nodes.map(node => [node.id, node.position])), edges, viewport, density,
  }))
}

function TaskCardNode({ data, selected }: NodeProps<TaskNode>) {
  const { item, openTask } = data
  return (
    <article className={`task-flow-node task-flow-node--${item.lane}${selected ? ' is-selected' : ''}`}>
      <NodeToolbar isVisible={data.toolbarVisible} position={Position.Top}>
        <Button size="small" type="primary" icon={<EnterOutlined />} onClick={() => openTask(item.openPath)}>打开处理</Button>
      </NodeToolbar>
      <Handle type="target" position={Position.Left} className="task-flow-node__handle" />
      <div className="task-flow-node__header">
        <div className="min-w-0">
          <div className="task-flow-node__type">{item.category}</div>
          <div className="task-flow-node__title" title={item.title}>{item.title}</div>
        </div>
        <Tag color={item.statusColor} className="m-0 shrink-0">{item.statusLabel}</Tag>
      </div>
      <div className="task-flow-node__body">
        {item.weight > 1 && <span className="task-flow-node__count">包含 {item.weight} 项待办</span>}
        {item.community && <span>{item.community}</span>}
        {item.deadline && <span>截止 {item.deadline}</span>}
        {item.description && <span className="task-flow-node__address">{item.description}</span>}
      </div>
      <div className="task-flow-node__footer">
        <span>{item.owner}</span>
        <button type="button" className="task-flow-node__open nodrag nopan" onClick={event => { event.stopPropagation(); openTask(item.openPath) }}>打开处理</button>
      </div>
      <Handle type="source" position={Position.Right} className="task-flow-node__handle" />
    </article>
  )
}

const NODE_TYPES = { task: memo(TaskCardNode) }

async function loadPersonTasks(inspector: string, passive: boolean): Promise<LoadedTaskFlowItems> {
  const results = await Promise.all(MOBILE_TASK_TYPES.map(async parserType => {
    const first = await listMobileTasks({ parser_type: parserType, scope: 'all', status: 'pending', inspectors: [inspector], sort: 'priority', page: 1, page_size: 50 }, { passive })
    const pageCount = Math.min(Math.ceil(first.total / 50), MAX_PERSON_TASKS_PER_TYPE / 50)
    const rest = pageCount > 1 ? await Promise.all(Array.from({ length: pageCount - 1 }, (_, index) => listMobileTasks({ parser_type: parserType, scope: 'all', status: 'pending', inspectors: [inspector], sort: 'priority', page: index + 2, page_size: 50 }, { passive }))) : []
    return { tasks: [first, ...rest].flatMap(page => page.data), total: first.total }
  }))
  const tasks = results.flatMap(result => result.tasks)
  return { items: tasks.map(personTaskItem), total: results.reduce((sum, result) => sum + result.total, 0), loadedTotal: tasks.length }
}

async function loadControlTasks(passive: boolean): Promise<LoadedTaskFlowItems> {
  const [analysisResults, photoResult, dispatchResult] = await Promise.all([
    Promise.all(MOBILE_TASK_TYPES.map(parserType => listMobileTasks({ parser_type: parserType, scope: 'all', status: 'all', review_stage: 'waiting_analysis', sort: 'priority', page: 1, page_size: 20 }, { passive }))),
    workflowApi.pendingPhotoRequests({ page: 1, page_size: MAX_CONTROL_PHOTO_NODES }, { passive }),
    getPoliceDispatchWorkbench({ passive }),
  ])
  const analysisItems = analysisResults.flatMap(result => result.data).slice(0, MAX_CONTROL_ANALYSIS_NODES).map(analysisTaskItem)
  const photoItems = photoResult.data.map(photoTaskItem)
  const dispatchItems = dispatchResult.batches.flatMap(dispatchBatchItems)
  const dispatchTotal = dispatchItems.reduce((sum, item) => sum + item.weight, 0)
  return {
    items: [...analysisItems, ...photoItems, ...dispatchItems],
    total: analysisResults.reduce((sum, result) => sum + result.total, 0) + photoResult.total + dispatchTotal,
    loadedTotal: analysisItems.length + photoItems.length + dispatchTotal,
  }
}

function TaskFlowLabContent() {
  const navigate = useNavigate()
  const mobile = useMobileViewport()
  const { fitView, getViewport } = useReactFlow<TaskNode>()
  const [view, setView] = useState<TaskFlowView>(() => localStorage.getItem(LAST_VIEW_KEY) === 'control' ? 'control' : 'person')
  const [density, setDensity] = useState<LayoutDensity>('standard')
  const [inspectors, setInspectors] = useState<MobileTaskFilterOption[]>([])
  const [selectedInspector, setSelectedInspector] = useState(() => localStorage.getItem(LAST_INSPECTOR_KEY) || '')
  const [keyword, setKeyword] = useState('')
  const debouncedKeyword = useDebouncedValue(keyword, 300)
  const [nodes, setNodes] = useState<TaskNode[]>([])
  const [edges, setEdges] = useState<Edge[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [availableTotal, setAvailableTotal] = useState(0)
  const [loadedTotal, setLoadedTotal] = useState(0)
  const [loadingInspectors, setLoadingInspectors] = useState(true)
  const [loadingTasks, setLoadingTasks] = useState(false)
  const [error, setError] = useState('')
  const requestId = useRef(0)
  const knownIds = useRef<Set<string> | null>(null)
  const nodesRef = useRef<TaskNode[]>([])
  const edgesRef = useRef<Edge[]>([])
  const densityRef = useRef<LayoutDensity>('standard')
  const contextKey = view === 'control' ? 'control' : selectedInspector ? `person:${selectedInspector}` : ''
  const canLoad = view === 'control' || Boolean(selectedInspector)

  useEffect(() => { nodesRef.current = nodes }, [nodes])
  useEffect(() => { edgesRef.current = edges }, [edges])
  useEffect(() => { densityRef.current = density }, [density])
  const openTask = useCallback((path: string) => navigate(path), [navigate])

  const loadInspectors = useCallback(async () => {
    setLoadingInspectors(true)
    try {
      const results = await Promise.all(MOBILE_TASK_TYPES.map(parserType => getMobileTaskFilterOptions(parserType, 'all')))
      const values = new Map<string, number>()
      results.flatMap(result => result.inspectors).forEach(option => { if (option.value && option.value !== '__empty__') values.set(option.value, (values.get(option.value) || 0) + option.count) })
      setInspectors([...values.entries()].map(([value, count]) => ({ value, label: value, count })).sort((a, b) => b.count - a.count))
    } catch (reason: any) { setError(reason?.response?.data?.detail || reason?.message || '核查人列表读取失败')
    } finally { setLoadingInspectors(false) }
  }, [])
  useEffect(() => { void loadInspectors() }, [loadInspectors])

  const refreshTasks = useCallback(async (passive = false) => {
    if (!canLoad || !contextKey) { setNodes([]); setEdges([]); setAvailableTotal(0); setLoadedTotal(0); return }
    const current = ++requestId.current
    if (!passive) setLoadingTasks(true)
    try {
      const result = view === 'control' ? await loadControlTasks(passive) : await loadPersonTasks(selectedInspector, passive)
      if (current !== requestId.current) return
      const saved = readLayout(contextKey)
      const nextDensity = saved.density || densityRef.current
      setDensity(nextDensity)
      const previous = new Map(nodesRef.current.map(node => [node.id, node.position]))
      const laidOut = layoutItems(result.items, nextDensity, previous)
      const nextNodes = laidOut.map(({ item, position }) => ({
        id: item.id, type: 'task' as const, position,
        data: { item, fresh: knownIds.current ? !knownIds.current.has(item.id) : false, toolbarVisible: selectedId === item.id, openTask },
      }))
      const ids = new Set(nextNodes.map(node => node.id))
      setNodes(nextNodes); setEdges(saved.edges.filter(edge => ids.has(edge.source) && ids.has(edge.target)))
      setAvailableTotal(result.total); setLoadedTotal(result.loadedTotal); setError('')
      knownIds.current = ids
      if (nextNodes.length && !saved.viewport) window.setTimeout(() => getViewport() && void fitView({ padding: 0.16, duration: 250 }), 40)
    } catch (reason: any) { if (current === requestId.current) setError(reason?.response?.data?.detail || reason?.message || '任务流读取失败')
    } finally { if (current === requestId.current) setLoadingTasks(false) }
  }, [canLoad, contextKey, fitView, getViewport, openTask, selectedId, selectedInspector, view])

  useEffect(() => {
    knownIds.current = null
    localStorage.setItem(LAST_VIEW_KEY, view)
    if (selectedInspector) localStorage.setItem(LAST_INSPECTOR_KEY, selectedInspector)
    void refreshTasks(false)
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void refreshTasks(true) }, AUTO_REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [refreshTasks, selectedInspector, view])

  const visibleNodes = useMemo(() => {
    const text = debouncedKeyword.trim().toLowerCase()
    if (!text) return nodes
    return nodes.filter(node => [node.data.item.category, node.data.item.title, node.data.item.community, node.data.item.owner, node.data.item.description].some(value => String(value || '').toLowerCase().includes(text)))
  }, [debouncedKeyword, nodes])
  const visibleIds = useMemo(() => new Set(visibleNodes.map(node => node.id)), [visibleNodes])
  const visibleEdges = useMemo(() => edges.filter(edge => visibleIds.has(edge.source) && visibleIds.has(edge.target)), [edges, visibleIds])
  const laneCounts = useMemo(() => TASK_FLOW_LANES.map(lane => ({ ...lane, count: visibleNodes.filter(node => node.data.item.lane === lane.key).reduce((sum, node) => sum + node.data.item.weight, 0) })), [visibleNodes])
  const processSteps = view === 'control' ? ['待办进入', '审核与处理', '发布或回传', '完成'] : ['收到任务', '核查处理', '协作等待', '完成或回流']

  const autoLayout = () => {
    const previous = new Map<string, { x: number; y: number }>()
    const next = layoutItems(nodes.map(node => node.data.item), density, previous).map(({ item, position }) => ({
      id: item.id, type: 'task' as const, position,
      data: { item, fresh: false, toolbarVisible: selectedId === item.id, openTask },
    }))
    setNodes(next)
    writeLayout(contextKey, next, edges, getViewport(), density)
    window.setTimeout(() => void fitView({ padding: 0.16, duration: 250 }), 30)
  }

  const onNodesChange = useCallback((changes: NodeChange<TaskNode>[]) => setNodes(current => applyNodeChanges(changes, current)), [])
  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    setEdges(current => {
      const next = applyEdgeChanges(changes, current)
      writeLayout(contextKey, nodesRef.current, next, getViewport(), densityRef.current)
      return next
    })
  }, [contextKey, getViewport])
  const onConnect = useCallback((connection: Connection) => {
    setEdges(current => {
      const next = addEdge({ ...connection, type: 'smoothstep', markerEnd: { type: MarkerType.ArrowClosed }, style: { strokeDasharray: '6 5' } }, current)
      writeLayout(contextKey, nodesRef.current, next, getViewport(), densityRef.current)
      return next
    })
  }, [contextKey, getViewport])

  return (
    <div className="task-flow-lab mx-auto max-w-[1680px] space-y-4 pb-4">
      <section className="app-card task-flow-lab__hero">
        <div><div className="task-flow-lab__eyebrow"><ExperimentOutlined /> 超级管理员内测</div><h1>我的任务流</h1><p>任务卡可以自由拖动、连线和排列。系统只提供初始布局和状态提示，不限制你的流程组织方式。</p></div>
      </section>
      <section className="app-card task-flow-lab__toolbar">
        <div className="task-flow-lab__filters">
          <Segmented value={view} options={[{ label: '网格员任务', value: 'person' }, { label: '基础管控', value: 'control' }]} onChange={value => setView(value as TaskFlowView)} />
          {view === 'person' && <Select showSearch loading={loadingInspectors} value={selectedInspector || undefined} placeholder="选择核查人" optionFilterProp="label" className="task-flow-lab__inspector" options={inspectors.map(option => ({ value: option.value, label: `${option.label} · ${option.count}条当前记录` }))} onChange={setSelectedInspector} />}
          <Input allowClear prefix={<SearchOutlined />} value={keyword} placeholder="搜索任务卡片" onChange={event => setKeyword(event.target.value)} />
        </div>
        <div className="task-flow-lab__summary">{laneCounts.map(lane => <span key={lane.key} className={`task-flow-lab__summary-item is-${lane.key}`}>{lane.label}<strong>{lane.count}</strong></span>)}</div>
      </section>
      {error && <Alert type="error" showIcon message={error} />}
      {availableTotal > loadedTotal && <Alert type="warning" showIcon message={`当前共有 ${availableTotal} 项待办，当前加载 ${loadedTotal} 项；批量任务已按业务聚合。`} />}
      {!canLoad ? <section className="app-card task-flow-lab__empty">{loadingInspectors ? <Skeleton active paragraph={{ rows: 4 }} /> : <Empty description="先选择一名核查人" />}</section>
        : mobile ? <section className="task-flow-lab__mobile-list">{visibleNodes.length ? visibleNodes.map(node => <button key={node.id} type="button" className={`app-card task-flow-mobile-card is-${node.data.item.lane}`} onClick={() => openTask(node.data.item.openPath)}><span>{node.data.item.category}</span><strong>{node.data.item.title}</strong><span>{node.data.item.community || node.data.item.description || '暂无补充信息'}</span></button>) : <div className="app-card p-8"><Empty description="当前筛选下没有待办" /></div>}</section>
        : <section className="app-card task-flow-lab__canvas-shell"><div className="task-flow-lab__canvas">{loadingTasks && !nodes.length ? <div className="task-flow-lab__loading"><Skeleton active paragraph={{ rows: 6 }} /></div> : <ReactFlow nodes={visibleNodes} edges={visibleEdges} nodeTypes={NODE_TYPES} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={onConnect} onNodeClick={(_, node) => setSelectedId(node.id)} onPaneClick={() => setSelectedId('')} onNodeDragStop={(_, node) => writeLayout(contextKey, nodesRef.current, edgesRef.current, getViewport(), densityRef.current)} onMoveEnd={(_, viewport) => writeLayout(contextKey, nodesRef.current, edgesRef.current, viewport, densityRef.current)} onNodeDoubleClick={(_, node) => openTask(node.data.item.openPath)} nodesDeletable={false} autoPanOnNodeDrag={false} minZoom={0.25} maxZoom={1.8} proOptions={{ hideAttribution: true }}><FlowPanel position="top-left" className="task-flow-canvas-toolbar"><Button size="small" icon={<ReloadOutlined />} loading={loadingTasks} onClick={() => void refreshTasks(false)}>刷新</Button><Button size="small" icon={<CompressOutlined />} onClick={autoLayout}>自动整理</Button><Button size="small" onClick={() => void fitView({ padding: 0.16, duration: 250 })}>适配当前任务</Button><Segmented size="small" value={density} options={[{ label: '紧凑', value: 'compact' }, { label: '标准', value: 'standard' }, { label: '宽松', value: 'comfortable' }]} onChange={value => { setDensity(value as LayoutDensity); writeLayout(contextKey, nodesRef.current, edgesRef.current, getViewport(), value as LayoutDensity) }} /></FlowPanel><FlowPanel position="top-center" className="task-flow-process-strip">{processSteps.map((step, index) => <span key={step}><b>{index + 1}</b>{step}{index < processSteps.length - 1 && <i aria-hidden="true">→</i>}</span>)}</FlowPanel><Background gap={24} size={1.2} /><MiniMap pannable zoomable /><Controls showInteractive={false} /></ReactFlow>}</div></section>}
    </div>
  )
}

export default function TaskFlowLab() { return <ReactFlowProvider><TaskFlowLabContent /></ReactFlowProvider> }
