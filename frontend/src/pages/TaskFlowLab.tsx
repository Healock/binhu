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
import {
  CompressOutlined,
  EnterOutlined,
  ExperimentOutlined,
  HolderOutlined,
  LeftOutlined,
  ReloadOutlined,
  RightOutlined,
  SearchOutlined,
} from '@ant-design/icons'
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
import {
  TASK_FLOW_LANES,
  defaultTaskFlowPosition,
  mergeTaskFlowInspectors,
  taskFlowLane,
  taskFlowLaneHeight,
  taskFlowLaneNodeId,
  taskFlowNodeId,
  type TaskFlowLane,
} from '../utils/taskFlow'

const AUTO_REFRESH_MS = 30_000
const MAX_PERSON_TASKS_PER_TYPE = 100
const MAX_CONTROL_ANALYSIS_NODES = 60
const MAX_CONTROL_PHOTO_NODES = 50
const LAST_INSPECTOR_KEY = 'binhu-task-flow-lab:last-inspector'
const LAST_VIEW_KEY = 'binhu-task-flow-lab:last-view'
const LAYOUT_KEY_PREFIX = 'binhu-task-flow-lab:layout-v5:'
const LANE_GAP = 28
const STACK_THRESHOLD = 6

type TaskFlowView = 'person' | 'control'
type LayoutDensity = 'compact' | 'standard' | 'comfortable'

const DENSITY_LAYOUTS: Record<LayoutDensity, { width: number; height: number }> = {
  compact: { width: 380, height: 460 },
  standard: { width: 460, height: 560 },
  comfortable: { width: 560, height: 680 },
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
  openTask: (path: string) => void
} & Record<string, unknown>

type LaneNodeData = {
  lane: TaskFlowLane
  label: string
  description: string
  count: number
} & Record<string, unknown>

type DeckNodeData = {
  lane: TaskFlowLane
  count: number
  nodeCount: number
  categories: Array<{ label: string; count: number }>
  activeItem: TaskFlowItem
  activeIndex: number
  toolbarVisible: boolean
  previous: (lane: TaskFlowLane) => void
  next: (lane: TaskFlowLane) => void
  open: (path: string) => void
} & Record<string, unknown>

type TaskNode = Node<TaskFlowNodeData, 'task'>
type LaneNode = Node<LaneNodeData, 'lane'>
type DeckNode = Node<DeckNodeData, 'deck'>
type FlowNode = TaskNode | LaneNode | DeckNode

interface SavedTaskFlowPosition {
  x: number
  y: number
  lane: TaskFlowLane
}

interface SavedTaskFlowLayout {
  positions: Record<string, SavedTaskFlowPosition>
  edges: Edge[]
  viewport: Viewport | null
  density: LayoutDensity
  activeCardByLane: Record<TaskFlowLane, string>
}

function emptyActiveCardByLane(): Record<TaskFlowLane, string> {
  return { ready: '', waiting: '', exception: '' }
}

interface LoadedTaskFlowItems {
  items: TaskFlowItem[]
  total: number
  loadedTotal: number
}

function layoutStorageKey(contextKey: string) {
  return `${LAYOUT_KEY_PREFIX}${encodeURIComponent(contextKey)}`
}

function readSavedLayout(contextKey: string): SavedTaskFlowLayout {
  try {
    const parsed = JSON.parse(localStorage.getItem(layoutStorageKey(contextKey)) || '{}')
    return {
      positions: parsed?.positions && typeof parsed.positions === 'object' ? parsed.positions : {},
      edges: Array.isArray(parsed?.edges) ? parsed.edges : [],
      viewport: parsed?.viewport
        && Number.isFinite(parsed.viewport.x)
        && Number.isFinite(parsed.viewport.y)
        && Number.isFinite(parsed.viewport.zoom)
        ? parsed.viewport
        : null,
      density: ['compact', 'standard', 'comfortable'].includes(parsed?.density)
        ? parsed.density
        : 'standard',
      activeCardByLane: {
        ready: String(parsed?.activeCardByLane?.ready || ''),
        waiting: String(parsed?.activeCardByLane?.waiting || ''),
        exception: String(parsed?.activeCardByLane?.exception || ''),
      },
    }
  } catch {
    return {
      positions: {}, edges: [], viewport: null, density: 'standard',
      activeCardByLane: emptyActiveCardByLane(),
    }
  }
}

function saveLayout(
  contextKey: string,
  nodes: TaskNode[],
  edges: Edge[],
  viewport: Viewport | null = null,
  density: LayoutDensity = 'standard',
  activeCardByLane: Record<TaskFlowLane, string> = emptyActiveCardByLane(),
) {
  if (!contextKey) return
  const positions = Object.fromEntries(nodes.map(node => [node.id, {
    ...node.position,
    lane: node.data.item.lane,
  }]))
  localStorage.setItem(layoutStorageKey(contextKey), JSON.stringify({
    positions,
    edges,
    viewport,
    density,
    activeCardByLane,
  }))
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
    id: taskFlowNodeId(task),
    lane,
    category: task.parser_type,
    title: task.summary.title || '未填写姓名',
    statusLabel: waitingForAnalysis ? '等待研判' : state.label,
    statusColor: waitingForAnalysis ? 'purple' : state.color,
    community: task.community,
    deadline: task.summary.deadline,
    description: task.summary.address,
    owner: task.inspector || '待分配',
    openPath: `/tasks/${encodeURIComponent(task.parser_type)}/${encodeURIComponent(task.row_key)}?scope=all`,
    weight: 1,
  }
}

function analysisTaskItem(task: MobileTaskItem): TaskFlowItem {
  return {
    id: `analysis:${taskFlowNodeId(task)}`,
    lane: 'ready',
    category: '网格核查研判',
    title: task.summary.title || '未填写姓名',
    statusLabel: '待研判',
    statusColor: 'purple',
    community: task.community,
    deadline: task.summary.deadline,
    description: task.summary.address || task.parser_type,
    owner: task.inspector || '核查人未填写',
    openPath: `/police-analysis/${encodeURIComponent(task.parser_type)}/${encodeURIComponent(task.row_key)}?scope=all`,
    weight: 1,
  }
}

function photoTaskItem(task: PendingPhotoRequest): TaskFlowItem {
  return {
    id: `photo:${task.id}`,
    lane: task.overdue ? 'exception' : 'ready',
    category: '调照片',
    title: task.subject_name || task.title || task.ticket_no,
    statusLabel: task.overdue ? '已逾期' : '待领取',
    statusColor: task.overdue ? 'red' : 'blue',
    community: task.community_name,
    deadline: '',
    description: task.source_label || task.ticket_no,
    owner: task.requester_name || '申请人未匹配',
    openPath: `/photo-tasks?ticket=${task.id}`,
    weight: 1,
  }
}

function dispatchBatchItems(batch: PoliceDispatchBatch): TaskFlowItem[] {
  const result: TaskFlowItem[] = []
  const regularReview = Math.max(0, batch.counts.pending_review - batch.counts.abnormal)
  if (regularReview) {
    result.push({
      id: `dispatch:${batch.id}:review`, lane: 'ready', category: '下发数据审核',
      title: `批次 #${batch.id} · ${batch.file_name}`, statusLabel: `待审核 ${regularReview}`,
      statusColor: 'orange', community: batch.sheet_name, deadline: '',
      description: '核对社区、登记情况、地址和下发去向', owner: batch.imported_by || '基础管控',
      openPath: `/police-tasks?batch=${batch.id}&status=pending_review&category=all`, weight: regularReview,
    })
  }
  if (batch.counts.abnormal) {
    result.push({
      id: `dispatch:${batch.id}:analysis`, lane: 'ready', category: '下发数据研判',
      title: `批次 #${batch.id} · ${batch.file_name}`, statusLabel: `待研判 ${batch.counts.abnormal}`,
      statusColor: 'purple', community: batch.sheet_name, deadline: '',
      description: '处理导入后无法直接确定去向的数据', owner: batch.imported_by || '基础管控',
      openPath: `/police-tasks?batch=${batch.id}&status=pending_review&category=manual`, weight: batch.counts.abnormal,
    })
  }
  if (batch.counts.pending_publish) {
    result.push({
      id: `dispatch:${batch.id}:publish`, lane: 'ready', category: '下发数据发布',
      title: `批次 #${batch.id} · ${batch.file_name}`, statusLabel: `待发布 ${batch.counts.pending_publish}`,
      statusColor: 'blue', community: batch.sheet_name, deadline: '',
      description: '审核完成，等待选择任务并发布到腾讯全链条', owner: batch.imported_by || '基础管控',
      openPath: `/police-tasks?batch=${batch.id}&status=pending_publish&category=all`, weight: batch.counts.pending_publish,
    })
  }
  const exceptionCount = batch.counts.conflict + batch.counts.needs_reconciliation + batch.counts.retryable
  if (exceptionCount) {
    const status = batch.counts.conflict
      ? 'conflict'
      : batch.counts.needs_reconciliation ? 'needs_reconciliation' : 'retryable'
    result.push({
      id: `dispatch:${batch.id}:exception`, lane: 'exception', category: '下发数据异常',
      title: `批次 #${batch.id} · ${batch.file_name}`, statusLabel: `需处理 ${exceptionCount}`,
      statusColor: 'red', community: batch.sheet_name, deadline: '',
      description: `冲突 ${batch.counts.conflict} · 待对账 ${batch.counts.needs_reconciliation} · 可重试 ${batch.counts.retryable}`,
      owner: '基础管控', openPath: `/police-tasks?batch=${batch.id}&status=${status}&category=all`,
      weight: exceptionCount,
    })
  }
  return result
}

function TaskCardNode({ data, selected }: NodeProps<TaskNode>) {
  const { item, fresh, openTask } = data
  return (
    <article className={`task-flow-node task-flow-node--${item.lane}${selected ? ' is-selected' : ''}${fresh ? ' is-fresh' : ''}`}>
      <Handle type="target" position={Position.Left} className="task-flow-node__handle" />
      <div className="task-flow-node__header">
        <span className="task-flow-node__drag-handle" title="拖动任务卡" aria-label="拖动任务卡">
          <HolderOutlined />
        </span>
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
        <button type="button" className="task-flow-node__open nodrag nopan" onClick={event => {
          event.stopPropagation()
          openTask(item.openPath)
        }}>打开处理</button>
      </div>
      <Handle type="source" position={Position.Right} className="task-flow-node__handle" />
    </article>
  )
}

function LaneGroupNode({ data }: NodeProps<LaneNode>) {
  return (
    <section className={`task-flow-group task-flow-group--${data.lane}`}>
      <div className="task-flow-group__header">
        <div><strong>{data.label}</strong><span>{data.description}</span></div>
        <b>{data.count}</b>
      </div>
    </section>
  )
}

function DeckCardNode({ data }: NodeProps<DeckNode>) {
  const item = data.activeItem
  return (
    <article className={`task-flow-deck task-flow-deck--${data.lane}`}>
      <NodeToolbar isVisible={data.toolbarVisible} position={Position.Top} className="task-flow-deck__toolbar">
        <Button size="small" icon={<LeftOutlined />} onClick={() => data.previous(data.lane)}>上一张</Button>
        <Button size="small" icon={<RightOutlined />} onClick={() => data.next(data.lane)}>下一张</Button>
        <Button size="small" type="primary" icon={<EnterOutlined />} onClick={() => data.open(item.openPath)}>打开处理</Button>
      </NodeToolbar>
      <Handle type="target" position={Position.Left} className="task-flow-node__handle" />
      <div className="task-flow-deck__layers" aria-hidden="true"><i /><i /></div>
      <div className="task-flow-deck__content">
        <div className="task-flow-deck__topline">
          <span className="task-flow-deck__eyebrow">任务卡组 · {data.activeIndex + 1}/{data.nodeCount}</span>
          <Tag color={item.statusColor} className="m-0">{item.statusLabel}</Tag>
        </div>
        <div className="task-flow-deck__active-title">{item.title}</div>
        <div className="task-flow-deck__active-meta">
          <span>{item.category}</span>
          {item.community && <span>{item.community}</span>}
          {item.description && <span>{item.description}</span>}
        </div>
        <div className="task-flow-deck__categories">
          {data.categories.slice(0, 4).map(category => (
            <span key={category.label}>{category.label}<b>{category.count}</b></span>
          ))}
        </div>
        <div className="task-flow-deck__actions">
          <Button icon={<LeftOutlined />} onClick={() => data.previous(data.lane)} aria-label="上一张任务" />
          <Button type="primary" className="flex-1" onClick={() => data.open(item.openPath)}>打开处理</Button>
          <Button icon={<RightOutlined />} onClick={() => data.next(data.lane)} aria-label="下一张任务" />
        </div>
      </div>
      <Handle type="source" position={Position.Right} className="task-flow-node__handle" />
    </article>
  )
}

const NODE_TYPES = {
  task: memo(TaskCardNode),
  lane: memo(LaneGroupNode),
  deck: memo(DeckCardNode),
}

async function loadTasksForInspector(inspector: string, passive: boolean): Promise<LoadedTaskFlowItems> {
  const results = await Promise.all(MOBILE_TASK_TYPES.map(async parserType => {
    const first = await listMobileTasks({
      parser_type: parserType, scope: 'all', status: 'pending', inspectors: [inspector],
      sort: 'priority', page: 1, page_size: 50,
    }, { passive })
    const pageCount = Math.min(Math.ceil(first.total / 50), MAX_PERSON_TASKS_PER_TYPE / 50)
    const remaining = pageCount > 1
      ? await Promise.all(Array.from({ length: pageCount - 1 }, (_, index) => listMobileTasks({
          parser_type: parserType, scope: 'all', status: 'pending', inspectors: [inspector],
          sort: 'priority', page: index + 2, page_size: 50,
        }, { passive })))
      : []
    return { tasks: [first, ...remaining].flatMap(page => page.data), total: first.total }
  }))
  const tasks = results.flatMap(result => result.tasks)
  return {
    items: tasks.map(personTaskItem),
    total: results.reduce((sum, result) => sum + result.total, 0),
    loadedTotal: tasks.length,
  }
}

async function loadControlTasks(passive: boolean): Promise<LoadedTaskFlowItems> {
  const [analysisResults, photoResult, dispatchResult] = await Promise.all([
    Promise.all(MOBILE_TASK_TYPES.map(parserType => listMobileTasks({
      parser_type: parserType, scope: 'all', status: 'all', review_stage: 'waiting_analysis',
      sort: 'priority', page: 1, page_size: 20,
    }, { passive }))),
    workflowApi.pendingPhotoRequests({ page: 1, page_size: MAX_CONTROL_PHOTO_NODES }, { passive }),
    getPoliceDispatchWorkbench({ passive }),
  ])
  const allAnalysis = analysisResults.flatMap(result => result.data)
  const analysisItems = allAnalysis.slice(0, MAX_CONTROL_ANALYSIS_NODES).map(analysisTaskItem)
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
  const { fitView, getViewport, setViewport } = useReactFlow<FlowNode>()
  const [view, setView] = useState<TaskFlowView>(() => (
    localStorage.getItem(LAST_VIEW_KEY) === 'control' ? 'control' : 'person'
  ))
  const [inspectors, setInspectors] = useState<MobileTaskFilterOption[]>([])
  const [selectedInspector, setSelectedInspector] = useState(() => localStorage.getItem(LAST_INSPECTOR_KEY) || '')
  const [keyword, setKeyword] = useState('')
  const debouncedKeyword = useDebouncedValue(keyword, 300)
  const [nodes, setNodes] = useState<TaskNode[]>([])
  const [edges, setEdges] = useState<Edge[]>([])
  const [density, setDensity] = useState<LayoutDensity>('standard')
  const [activeCardByLane, setActiveCardByLane] = useState<Record<TaskFlowLane, string>>(
    () => emptyActiveCardByLane(),
  )
  const [selectedDeck, setSelectedDeck] = useState<TaskFlowLane | null>(null)
  const [availableTotal, setAvailableTotal] = useState(0)
  const [loadedTotal, setLoadedTotal] = useState(0)
  const [loadingInspectors, setLoadingInspectors] = useState(true)
  const [loadingTasks, setLoadingTasks] = useState(false)
  const [error, setError] = useState('')
  const requestId = useRef(0)
  const knownNodeIds = useRef<Set<string> | null>(null)
  const fittedContext = useRef('')
  const freshTimer = useRef<number | null>(null)
  const nodesRef = useRef<TaskNode[]>([])
  const edgesRef = useRef<Edge[]>([])
  const densityRef = useRef<LayoutDensity>('standard')
  const activeCardByLaneRef = useRef<Record<TaskFlowLane, string>>(emptyActiveCardByLane())
  const layoutContext = view === 'control' ? 'control' : selectedInspector ? `person:${selectedInspector}` : ''
  const canLoad = view === 'control' || Boolean(selectedInspector)

  useEffect(() => { nodesRef.current = nodes }, [nodes])
  useEffect(() => { edgesRef.current = edges }, [edges])
  useEffect(() => { densityRef.current = density }, [density])
  useEffect(() => { activeCardByLaneRef.current = activeCardByLane }, [activeCardByLane])
  const openTask = useCallback((path: string) => navigate(path), [navigate])

  const loadInspectors = useCallback(async () => {
    setLoadingInspectors(true)
    try {
      const results = await Promise.all(MOBILE_TASK_TYPES.map(parserType => getMobileTaskFilterOptions(parserType, 'all')))
      const merged = mergeTaskFlowInspectors(results.map(result => result.inspectors))
      setInspectors(merged)
      if (selectedInspector && !merged.some(option => option.value === selectedInspector)) {
        setSelectedInspector('')
        localStorage.removeItem(LAST_INSPECTOR_KEY)
      }
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || reason?.message || '核查人列表读取失败')
    } finally {
      setLoadingInspectors(false)
    }
  }, [selectedInspector])

  useEffect(() => { void loadInspectors() }, [loadInspectors])

  const refreshTasks = useCallback(async (passive = false) => {
    if (!canLoad || !layoutContext) {
      setNodes([]); setEdges([]); setAvailableTotal(0); setLoadedTotal(0); return
    }
    const currentRequest = ++requestId.current
    if (!passive) setLoadingTasks(true)
    try {
      const result = view === 'control'
        ? await loadControlTasks(passive)
        : await loadTasksForInspector(selectedInspector, passive)
      if (currentRequest !== requestId.current) return
      const saved = readSavedLayout(layoutContext)
      const previousNodes = new Map(nodesRef.current.map(node => [node.id, node]))
      const laneIndexes: Record<TaskFlowLane, number> = { ready: 0, waiting: 0, exception: 0 }
      const currentIds = new Set(result.items.map(item => item.id))
      const newIds = knownNodeIds.current
        ? new Set([...currentIds].filter(id => !knownNodeIds.current?.has(id)))
        : new Set<string>()
      const nextNodes = result.items.map<TaskNode>(item => {
        const previous = previousNodes.get(item.id)
        const savedPosition = saved.positions[item.id]
        const position = previous?.data.item.lane === item.lane
          ? previous.position
          : savedPosition?.lane === item.lane
            ? { x: savedPosition.x, y: savedPosition.y }
            : defaultTaskFlowPosition(item.lane, laneIndexes[item.lane])
        laneIndexes[item.lane] += 1
        return {
          id: item.id, type: 'task', parentId: taskFlowLaneNodeId(item.lane), extent: 'parent',
          expandParent: false, position, zIndex: 1, dragHandle: '.task-flow-node__drag-handle',
          data: { item, fresh: newIds.has(item.id), openTask },
        }
      })
      const contextChanged = fittedContext.current !== layoutContext
      const nextDensity = contextChanged ? saved.density : densityRef.current
      const activeSource = contextChanged ? saved.activeCardByLane : activeCardByLaneRef.current
      const nextActiveCardByLane = Object.fromEntries(TASK_FLOW_LANES.map(({ key }) => {
        const laneTasks = nextNodes.filter(node => node.data.item.lane === key)
        const selected = laneTasks.some(node => node.id === activeSource[key])
          ? activeSource[key]
          : laneTasks[0]?.id || ''
        return [key, selected]
      })) as Record<TaskFlowLane, string>
      const nextEdges = saved.edges.filter(edge => currentIds.has(edge.source) && currentIds.has(edge.target))
      knownNodeIds.current = currentIds
      setNodes(nextNodes)
      setEdges(nextEdges)
      setDensity(nextDensity)
      setActiveCardByLane(nextActiveCardByLane)
      if (contextChanged) setSelectedDeck(null)
      setAvailableTotal(result.total)
      setLoadedTotal(result.loadedTotal)
      setError('')
      if (newIds.size > 0) {
        message.info(`${view === 'control' ? '基础管控' : selectedInspector}新增 ${newIds.size} 个任务节点`)
        if (freshTimer.current) window.clearTimeout(freshTimer.current)
        freshTimer.current = window.setTimeout(() => {
          setNodes(current => current.map(node => ({ ...node, data: { ...node.data, fresh: false } })))
        }, 8000)
      }
      if (contextChanged && nextNodes.length > 0) {
        fittedContext.current = layoutContext
        window.setTimeout(() => {
          if (saved.viewport) void setViewport(saved.viewport, { duration: 0 })
          else void fitView({ padding: 0.08, duration: 350 })
        }, 50)
      }
    } catch (reason: any) {
      if (currentRequest === requestId.current) setError(reason?.response?.data?.detail || reason?.message || '任务流读取失败')
    } finally {
      if (currentRequest === requestId.current) setLoadingTasks(false)
    }
  }, [canLoad, fitView, layoutContext, openTask, selectedInspector, setViewport, view])

  useEffect(() => {
    knownNodeIds.current = null
    fittedContext.current = ''
    localStorage.setItem(LAST_VIEW_KEY, view)
    if (selectedInspector) localStorage.setItem(LAST_INSPECTOR_KEY, selectedInspector)
    void refreshTasks(false)
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refreshTasks(true)
    }, AUTO_REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [refreshTasks, selectedInspector, view])

  useEffect(() => () => { if (freshTimer.current) window.clearTimeout(freshTimer.current) }, [])

  const visibleNodes = useMemo(() => {
    const normalized = debouncedKeyword.trim().toLowerCase()
    if (!normalized) return nodes
    return nodes.filter(node => {
      const item = node.data.item
      return [item.category, item.title, item.community, item.owner, item.description]
        .some(value => String(value || '').toLowerCase().includes(normalized))
    })
  }, [debouncedKeyword, nodes])
  const persistViewState = useCallback((
    nextActive: Record<TaskFlowLane, string>,
    nextDensity = densityRef.current,
  ) => {
    saveLayout(
      layoutContext,
      nodesRef.current,
      edgesRef.current,
      getViewport(),
      nextDensity,
      nextActive,
    )
  }, [getViewport, layoutContext])
  const rotateCard = useCallback((lane: TaskFlowLane, direction: -1 | 1) => {
    const candidates = nodesRef.current.filter(node => node.data.item.lane === lane)
    if (!candidates.length) return
    setActiveCardByLane(current => {
      const currentIndex = Math.max(0, candidates.findIndex(node => node.id === current[lane]))
      const nextIndex = (currentIndex + direction + candidates.length) % candidates.length
      const next = { ...current, [lane]: candidates[nextIndex].id }
      window.setTimeout(() => persistViewState(next), 0)
      return next
    })
  }, [persistViewState])
  const changeDensity = useCallback((nextDensity: LayoutDensity) => {
    setDensity(nextDensity)
    window.setTimeout(() => persistViewState(activeCardByLaneRef.current, nextDensity), 0)
  }, [persistViewState])
  const laneCounts = useMemo(() => TASK_FLOW_LANES.map(lane => ({
    ...lane,
    count: visibleNodes.filter(node => node.data.item.lane === lane.key)
      .reduce((sum, node) => sum + node.data.item.weight, 0),
  })), [visibleNodes])
  const nodesByLane = useMemo(() => Object.fromEntries(TASK_FLOW_LANES.map(({ key }) => [
    key,
    visibleNodes.filter(node => node.data.item.lane === key),
  ])) as Record<TaskFlowLane, TaskNode[]>, [visibleNodes])
  const stackedLanes = useMemo(() => new Set(TASK_FLOW_LANES
    .filter(({ key }) => !debouncedKeyword.trim() && nodesByLane[key].length > STACK_THRESHOLD)
    .map(({ key }) => key)), [debouncedKeyword, nodesByLane])
  const displayedTaskNodes = useMemo(() => visibleNodes.filter(node => (
    !stackedLanes.has(node.data.item.lane)
  )), [stackedLanes, visibleNodes])
  const deckNodes = useMemo<DeckNode[]>(() => TASK_FLOW_LANES.flatMap(({ key }) => {
    if (!stackedLanes.has(key)) return []
    const laneTasks = nodesByLane[key]
    const activeIndex = Math.max(0, laneTasks.findIndex(node => node.id === activeCardByLane[key]))
    const activeItem = laneTasks[activeIndex]?.data.item || laneTasks[0]?.data.item
    if (!activeItem) return []
    const categoryCounts = new Map<string, number>()
    laneTasks.forEach(node => {
      const category = node.data.item.category
      categoryCounts.set(category, (categoryCounts.get(category) || 0) + node.data.item.weight)
    })
    return [{
      id: `task-flow-deck-${key}`,
      type: 'deck',
      parentId: taskFlowLaneNodeId(key),
      extent: 'parent',
      position: { x: 20, y: 64 },
      draggable: false,
      deletable: false,
      zIndex: 1,
      data: {
        lane: key,
        count: laneTasks.reduce((sum, node) => sum + node.data.item.weight, 0),
        nodeCount: laneTasks.length,
        activeItem,
        activeIndex,
        categories: [...categoryCounts.entries()]
          .map(([label, count]) => ({ label, count }))
          .sort((left, right) => right.count - left.count),
        toolbarVisible: selectedDeck === key,
        previous: lane => rotateCard(lane, -1),
        next: lane => rotateCard(lane, 1),
        open: openTask,
      },
    }]
  }), [activeCardByLane, nodesByLane, openTask, rotateCard, selectedDeck, stackedLanes])
  const laneMinHeights = useMemo(() => Object.fromEntries(TASK_FLOW_LANES.map(({ key }) => [
    key,
    taskFlowLaneHeight(displayedTaskNodes
      .filter(node => node.data.item.lane === key)
      .map(node => ({ lane: key, position: node.position }))),
  ])) as Record<TaskFlowLane, number>, [displayedTaskNodes])
  const laneNodes = useMemo<LaneNode[]>(() => {
    let x = 0
    return laneCounts.map(lane => {
      const preset = DENSITY_LAYOUTS[density]
      const width = preset.width
      const height = Math.max(preset.height, laneMinHeights[lane.key])
      const node: LaneNode = {
        id: taskFlowLaneNodeId(lane.key), type: 'lane',
        position: { x, y: 0 },
        className: 'task-flow-lane--passive',
        draggable: false, selectable: false, connectable: false, deletable: false, zIndex: 0,
        style: { width, height },
        data: {
          lane: lane.key,
          label: lane.label,
          description: lane.description,
          count: lane.count,
        },
      }
      x += width + LANE_GAP
      return node
    })
  }, [density, laneCounts, laneMinHeights])
  const processSteps = useMemo(() => view === 'control'
    ? ['待办进入', '审核与处理', '发布或回传', '完成']
    : ['收到任务', '核查处理', '协作等待', '完成或回流'], [view])
  const displayedIds = useMemo(() => new Set([
    ...displayedTaskNodes.map(node => node.id),
    ...deckNodes.map(node => node.id),
  ]), [deckNodes, displayedTaskNodes])
  const visibleManualEdges = useMemo(() => edges.filter(edge => (
    displayedIds.has(edge.source) && displayedIds.has(edge.target)
  )), [displayedIds, edges])
  const flowNodes = useMemo<FlowNode[]>(() => [
    ...laneNodes,
    ...deckNodes,
    ...displayedTaskNodes,
  ], [deckNodes, displayedTaskNodes, laneNodes])
  const flowEdges = visibleManualEdges

  const onNodesChange = useCallback((changes: NodeChange<FlowNode>[]) => {
    const taskChanges = changes.filter(change => !change.id.startsWith('task-flow-lane-')) as NodeChange<TaskNode>[]
    if (taskChanges.length) setNodes(current => applyNodeChanges(taskChanges, current))
  }, [])
  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    setEdges(current => {
      const next = applyEdgeChanges(changes, current)
      saveLayout(
        layoutContext, nodes, next, getViewport(),
        densityRef.current, activeCardByLaneRef.current,
      )
      return next
    })
  }, [getViewport, layoutContext, nodes])
  const onConnect = useCallback((connection: Connection) => {
    setEdges(current => {
      const next = addEdge({ ...connection, type: 'smoothstep', markerEnd: { type: MarkerType.ArrowClosed }, style: { strokeDasharray: '6 5' } }, current)
      saveLayout(
        layoutContext, nodes, next, getViewport(),
        densityRef.current, activeCardByLaneRef.current,
      )
      return next
    })
  }, [getViewport, layoutContext, nodes])
  const resetLayout = () => {
    const laneIndexes: Record<TaskFlowLane, number> = { ready: 0, waiting: 0, exception: 0 }
    const next = nodes.map(node => {
      const lane = node.data.item.lane
      const position = defaultTaskFlowPosition(lane, laneIndexes[lane])
      laneIndexes[lane] += 1
      return { ...node, position }
    })
    setNodes(next)
    saveLayout(
      layoutContext, next, edges, getViewport(),
      densityRef.current, activeCardByLaneRef.current,
    )
    window.setTimeout(() => fitView({ padding: 0.08, duration: 350 }), 50)
  }

  return (
    <div className="task-flow-lab mx-auto max-w-[1680px] space-y-4 pb-4">
      <section className="app-card task-flow-lab__hero">
        <div>
          <div className="task-flow-lab__eyebrow"><ExperimentOutlined /> 超级管理员内测</div>
          <h1>我的任务流</h1>
          <p>{view === 'control'
            ? '集中查看基础管控当前需要处理的网格研判、调照片、下发审核、研判、发布和异常任务。'
            : '选择一名核查人，平台会把其现有流口待办自动投放到沙盒。'} 节点只能在系统判定的区域内移动，虚线不会改变真实业务流程。</p>
        </div>
      </section>

      <section className="app-card task-flow-lab__toolbar">
        <div className="task-flow-lab__filters">
          <Segmented value={view} options={[
            { label: '网格员任务', value: 'person' },
            { label: '基础管控', value: 'control' },
          ]} onChange={value => setView(value as TaskFlowView)} />
          {view === 'person' && (
            <Select showSearch loading={loadingInspectors} value={selectedInspector || undefined}
              placeholder="选择核查人开始内测" optionFilterProp="label" className="task-flow-lab__inspector"
              options={inspectors.map(option => ({ value: option.value, label: `${option.label} · ${option.count}条当前记录` }))}
              onChange={setSelectedInspector} />
          )}
          <Input allowClear prefix={<SearchOutlined />} value={keyword} placeholder="筛选当前沙盒中的任务"
            onChange={event => setKeyword(event.target.value)} />
        </div>
        <div className="task-flow-lab__summary">
          {laneCounts.map(lane => (
            <span key={lane.key} className={`task-flow-lab__summary-item is-${lane.key}`} title={lane.description}>
              {lane.label}<strong>{lane.count}</strong>
            </span>
          ))}
        </div>
      </section>

      {error && <Alert type="error" showIcon message={error} />}
      {availableTotal > loadedTotal && (
        <Alert type="warning" showIcon
          message={`当前共有 ${availableTotal} 项待办，内测画布已加载其中 ${loadedTotal} 项；批量任务已按下发批次聚合，其余高数量明细请进入原工作台处理。`} />
      )}

      {!canLoad ? (
        <section className="app-card task-flow-lab__empty">
          {loadingInspectors ? <Skeleton active paragraph={{ rows: 4 }} /> : <Empty description="先选择一名核查人，查看任务如何自动进入个人沙盒" />}
        </section>
      ) : mobile ? (
        <section className="task-flow-lab__mobile-list">
          {visibleNodes.length ? visibleNodes.map(node => (
            <button key={node.id} type="button" className={`app-card task-flow-mobile-card is-${node.data.item.lane}`}
              onClick={() => openTask(node.data.item.openPath)}>
              <span className="task-flow-mobile-card__type">{node.data.item.category}</span>
              <strong>{node.data.item.title}</strong>
              <span>{node.data.item.community || node.data.item.description || '暂无补充信息'}</span>
            </button>
          )) : <div className="app-card p-8"><Empty description="当前筛选下没有待办" /></div>}
        </section>
      ) : (
        <section className="app-card task-flow-lab__canvas-shell">
          <div className="task-flow-lab__canvas">
            {loadingTasks && !nodes.length ? <div className="task-flow-lab__loading"><Skeleton active paragraph={{ rows: 6 }} /></div> : (
              <ReactFlow<FlowNode> nodes={flowNodes} edges={flowEdges} nodeTypes={NODE_TYPES}
                onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={onConnect}
                onNodeClick={(_, node) => {
                  setSelectedDeck(node.type === 'deck' ? node.data.lane : null)
                }}
                onPaneClick={() => setSelectedDeck(null)}
                onNodeDragStop={(_, node) => {
                  if (node.type !== 'task') return
                  const next = nodes.map(item => item.id === node.id ? { ...item, position: node.position } : item)
                  setNodes(next)
                  saveLayout(
                    layoutContext, next, edges, getViewport(),
                    densityRef.current, activeCardByLaneRef.current,
                  )
                }}
                onMoveEnd={(_, viewport) => saveLayout(
                  layoutContext,
                  nodesRef.current,
                  edgesRef.current,
                  viewport,
                  densityRef.current,
                  activeCardByLaneRef.current,
                )}
                onNodeDoubleClick={(_, node) => {
                  if (node.type === 'task') openTask(node.data.item.openPath)
                  if (node.type === 'deck') openTask(node.data.activeItem.openPath)
                }}
                nodesDeletable={false} autoPanOnNodeDrag={false} minZoom={0.2} maxZoom={1.6}
                deleteKeyCode={['Backspace', 'Delete']}
                proOptions={{ hideAttribution: true }}>
                <FlowPanel position="top-left" className="task-flow-canvas-toolbar">
                  <Button size="small" icon={<ReloadOutlined />} loading={loadingTasks}
                    onClick={() => void refreshTasks(false)}>刷新</Button>
                  <Button size="small" icon={<CompressOutlined />} onClick={resetLayout}>自动布局</Button>
                  <Segmented
                    size="small"
                    value={density}
                    options={[
                      { label: '紧凑', value: 'compact' },
                      { label: '标准', value: 'standard' },
                      { label: '宽松', value: 'comfortable' },
                    ]}
                    onChange={value => changeDensity(value as LayoutDensity)}
                  />
                </FlowPanel>
                <FlowPanel position="top-center" className="task-flow-process-strip">
                  {processSteps.map((step, index) => (
                    <span key={step}>
                      <b>{index + 1}</b>{step}
                      {index < processSteps.length - 1 && <i aria-hidden="true">→</i>}
                    </span>
                  ))}
                </FlowPanel>
                <Background gap={24} size={1.2} />
                <MiniMap pannable zoomable nodeColor={node => {
                  if (node.type === 'lane') return 'rgba(148, 163, 184, 0.16)'
                  if (node.type === 'deck') {
                    const lane = (node.data as DeckNodeData).lane
                    return lane === 'ready' ? '#2563eb' : lane === 'waiting' ? '#d97706' : '#dc2626'
                  }
                  const lane = (node.data as TaskFlowNodeData).item.lane
                  return lane === 'ready' ? '#2563eb' : lane === 'waiting' ? '#d97706' : '#dc2626'
                }} />
                <Controls showInteractive={false} />
              </ReactFlow>
            )}
          </div>
        </section>
      )}
    </div>
  )
}

export default function TaskFlowLab() {
  return <ReactFlowProvider><TaskFlowLabContent /></ReactFlowProvider>
}
