import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  NodeResizer,
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
  AppstoreOutlined,
  CompressOutlined,
  ExperimentOutlined,
  PartitionOutlined,
  ReloadOutlined,
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
const LAYOUT_KEY_PREFIX = 'binhu-task-flow-lab:layout-v4:'
const LANE_WIDTH = 460
const LANE_GAP = 28
const LANE_MIN_WIDTH = 340
const LANE_MIN_HEIGHT = 320
const LANE_Y = 190
const STACK_THRESHOLD = 6
const MAX_DRAWN_PER_LANE = 3

type TaskFlowView = 'person' | 'control'

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
  resizeEnabled: boolean
  minHeight: number
  resizeLane: (lane: TaskFlowLane, width: number, height: number, finished: boolean) => void
} & Record<string, unknown>

type DeckNodeData = {
  lane: TaskFlowLane
  count: number
  nodeCount: number
  categories: Array<{ label: string; count: number }>
  toolbarVisible: boolean
  drawOne: (lane: TaskFlowLane) => void
  drawThree: (lane: TaskFlowLane) => void
  collapse: (lane: TaskFlowLane) => void
} & Record<string, unknown>

type WorkflowNodeData = {
  label: string
  description: string
  tone: 'blue' | 'amber' | 'green' | 'red'
} & Record<string, unknown>

type TaskNode = Node<TaskFlowNodeData, 'task'>
type LaneNode = Node<LaneNodeData, 'lane'>
type DeckNode = Node<DeckNodeData, 'deck'>
type WorkflowNode = Node<WorkflowNodeData, 'workflow'>
type FlowNode = TaskNode | LaneNode | DeckNode | WorkflowNode

interface SavedTaskFlowPosition {
  x: number
  y: number
  lane: TaskFlowLane
}

interface SavedTaskFlowLayout {
  positions: Record<string, SavedTaskFlowPosition>
  edges: Edge[]
  viewport: Viewport | null
  laneSizes: Record<TaskFlowLane, { width: number; height: number }> | null
  drawnByLane: Record<TaskFlowLane, string[]>
}

type LaneSizes = Record<TaskFlowLane, { width: number; height: number }>

function defaultLaneSizes(height = 560): LaneSizes {
  return {
    ready: { width: LANE_WIDTH, height },
    waiting: { width: LANE_WIDTH, height },
    exception: { width: LANE_WIDTH, height },
  }
}

function emptyDrawnByLane(): Record<TaskFlowLane, string[]> {
  return { ready: [], waiting: [], exception: [] }
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
      laneSizes: parsed?.laneSizes && typeof parsed.laneSizes === 'object'
        ? parsed.laneSizes
        : null,
      drawnByLane: {
        ready: Array.isArray(parsed?.drawnByLane?.ready) ? parsed.drawnByLane.ready : [],
        waiting: Array.isArray(parsed?.drawnByLane?.waiting) ? parsed.drawnByLane.waiting : [],
        exception: Array.isArray(parsed?.drawnByLane?.exception) ? parsed.drawnByLane.exception : [],
      },
    }
  } catch {
    return {
      positions: {}, edges: [], viewport: null, laneSizes: null,
      drawnByLane: emptyDrawnByLane(),
    }
  }
}

function saveLayout(
  contextKey: string,
  nodes: TaskNode[],
  edges: Edge[],
  viewport: Viewport | null = null,
  laneSizes: LaneSizes | null = null,
  drawnByLane: Record<TaskFlowLane, string[]> | null = null,
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
    laneSizes,
    drawnByLane,
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
    <section className={`task-flow-group task-flow-group--${data.lane}${data.resizeEnabled ? ' is-resizing' : ''}`}>
      <NodeResizer
        isVisible={data.resizeEnabled}
        minWidth={LANE_MIN_WIDTH}
        minHeight={Math.max(LANE_MIN_HEIGHT, data.minHeight)}
        lineClassName="task-flow-group__resize-line"
        handleClassName="task-flow-group__resize-handle"
        onResize={(_, size) => data.resizeLane(data.lane, size.width, size.height, false)}
        onResizeEnd={(_, size) => data.resizeLane(data.lane, size.width, size.height, true)}
      />
      <div className="task-flow-group__header">
        <div><strong>{data.label}</strong><span>{data.description}</span></div>
        <b>{data.count}</b>
      </div>
    </section>
  )
}

function DeckCardNode({ data }: NodeProps<DeckNode>) {
  return (
    <article className={`task-flow-deck task-flow-deck--${data.lane}`}>
      <NodeToolbar isVisible={data.toolbarVisible} position={Position.Top} className="task-flow-deck__toolbar">
        <Button size="small" onClick={() => data.drawOne(data.lane)}>抽取一项</Button>
        <Button size="small" onClick={() => data.drawThree(data.lane)}>抽取三项</Button>
        <Button size="small" onClick={() => data.collapse(data.lane)}>全部收起</Button>
      </NodeToolbar>
      <Handle type="target" position={Position.Left} className="task-flow-node__handle" />
      <div className="task-flow-deck__layers" aria-hidden="true"><i /><i /></div>
      <div className="task-flow-deck__content">
        <div className="task-flow-deck__eyebrow">任务卡组</div>
        <div className="task-flow-deck__headline">
          <strong>{data.count}</strong>
          <span>项待办</span>
        </div>
        <div className="task-flow-deck__categories">
          {data.categories.slice(0, 4).map(category => (
            <span key={category.label}>{category.label}<b>{category.count}</b></span>
          ))}
        </div>
        <Button type="primary" block onClick={() => data.drawOne(data.lane)}>抽取下一项</Button>
      </div>
      <Handle type="source" position={Position.Right} className="task-flow-node__handle" />
    </article>
  )
}

function WorkflowGuideNode({ data }: NodeProps<WorkflowNode>) {
  return (
    <article className={`task-flow-workflow-node is-${data.tone}`}>
      <Handle type="target" position={Position.Left} className="task-flow-workflow-node__handle" />
      <strong>{data.label}</strong>
      <span>{data.description}</span>
      <Handle type="source" position={Position.Right} className="task-flow-workflow-node__handle" />
    </article>
  )
}

const NODE_TYPES = {
  task: memo(TaskCardNode),
  lane: memo(LaneGroupNode),
  deck: memo(DeckCardNode),
  workflow: memo(WorkflowGuideNode),
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
  const [laneSizes, setLaneSizes] = useState<LaneSizes>(() => defaultLaneSizes())
  const [drawnByLane, setDrawnByLane] = useState<Record<TaskFlowLane, string[]>>(
    () => emptyDrawnByLane(),
  )
  const [selectedDeck, setSelectedDeck] = useState<TaskFlowLane | null>(null)
  const [resizeMode, setResizeMode] = useState(false)
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
  const laneSizesRef = useRef<LaneSizes>(defaultLaneSizes())
  const drawnByLaneRef = useRef<Record<TaskFlowLane, string[]>>(emptyDrawnByLane())
  const layoutContext = view === 'control' ? 'control' : selectedInspector ? `person:${selectedInspector}` : ''
  const canLoad = view === 'control' || Boolean(selectedInspector)

  useEffect(() => { nodesRef.current = nodes }, [nodes])
  useEffect(() => { edgesRef.current = edges }, [edges])
  useEffect(() => { laneSizesRef.current = laneSizes }, [laneSizes])
  useEffect(() => { drawnByLaneRef.current = drawnByLane }, [drawnByLane])
  useEffect(() => { setResizeMode(false) }, [layoutContext])
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
          expandParent: false, position, zIndex: 1,
          data: { item, fresh: newIds.has(item.id), openTask },
        }
      })
      const contextChanged = fittedContext.current !== layoutContext
      const sizeSource = contextChanged && saved.laneSizes
        ? saved.laneSizes
        : laneSizesRef.current
      const drawnSource = contextChanged ? saved.drawnByLane : drawnByLaneRef.current
      const nextDrawnByLane = Object.fromEntries(TASK_FLOW_LANES.map(({ key }) => [
        key,
        (drawnSource[key] || [])
          .filter(id => currentIds.has(id) && nextNodes.some(node => (
            node.id === id && node.data.item.lane === key
          )))
          .slice(0, MAX_DRAWN_PER_LANE),
      ])) as Record<TaskFlowLane, string[]>
      const nextLaneSizes = Object.fromEntries(TASK_FLOW_LANES.map(({ key }) => {
        const laneTasks = nextNodes.filter(node => node.data.item.lane === key)
        const compactTasks = laneTasks.length > STACK_THRESHOLD
          ? laneTasks.filter(node => nextDrawnByLane[key].includes(node.id))
          : laneTasks
        const contentHeight = taskFlowLaneHeight(compactTasks
          .map(node => ({ lane: key, position: node.position })))
        const stored = sizeSource?.[key]
        return [key, {
          width: Math.max(LANE_MIN_WIDTH, Number(stored?.width || LANE_WIDTH)),
          height: Math.max(LANE_MIN_HEIGHT, contentHeight, Number(stored?.height || 560)),
        }]
      })) as LaneSizes
      const nextEdges = saved.edges.filter(edge => currentIds.has(edge.source) && currentIds.has(edge.target))
      knownNodeIds.current = currentIds
      setNodes(nextNodes)
      setEdges(nextEdges)
      setLaneSizes(nextLaneSizes)
      setDrawnByLane(nextDrawnByLane)
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
  const persistDrawn = useCallback((next: Record<TaskFlowLane, string[]>) => {
    saveLayout(
      layoutContext,
      nodesRef.current,
      edgesRef.current,
      getViewport(),
      laneSizesRef.current,
      next,
    )
  }, [getViewport, layoutContext])
  const drawTasks = useCallback((lane: TaskFlowLane, amount: number) => {
    const candidates = nodesRef.current.filter(node => node.data.item.lane === lane)
    setDrawnByLane(current => {
      const existing = current[lane].filter(id => candidates.some(node => node.id === id))
      const capacity = Math.max(0, MAX_DRAWN_PER_LANE - existing.length)
      const additions = candidates
        .filter(node => !existing.includes(node.id))
        .slice(0, Math.min(amount, capacity))
      const laneIds = [...existing, ...additions.map(node => node.id)]
        .slice(0, MAX_DRAWN_PER_LANE)
      const next = { ...current, [lane]: laneIds }
      if (additions.length) {
        setNodes(nodeList => nodeList.map(node => {
          const drawnIndex = laneIds.indexOf(node.id)
          if (drawnIndex < 0 || !additions.some(item => item.id === node.id)) return node
          return {
            ...node,
            position: { x: 20, y: 260 + drawnIndex * 196 },
          }
        }))
      }
      window.setTimeout(() => persistDrawn(next), 0)
      return next
    })
  }, [persistDrawn])
  const collapseLane = useCallback((lane: TaskFlowLane) => {
    setDrawnByLane(current => {
      const next = { ...current, [lane]: [] }
      window.setTimeout(() => persistDrawn(next), 0)
      return next
    })
  }, [persistDrawn])
  const collapseAllDecks = useCallback(() => {
    const next = emptyDrawnByLane()
    setDrawnByLane(next)
    setSelectedDeck(null)
    window.setTimeout(() => persistDrawn(next), 0)
  }, [persistDrawn])
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
    || drawnByLane[node.data.item.lane].includes(node.id)
  )), [drawnByLane, stackedLanes, visibleNodes])
  const deckNodes = useMemo<DeckNode[]>(() => TASK_FLOW_LANES.flatMap(({ key }) => {
    if (!stackedLanes.has(key)) return []
    const laneTasks = nodesByLane[key]
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
        categories: [...categoryCounts.entries()]
          .map(([label, count]) => ({ label, count }))
          .sort((left, right) => right.count - left.count),
        toolbarVisible: selectedDeck === key,
        drawOne: lane => drawTasks(lane, 1),
        drawThree: lane => drawTasks(lane, MAX_DRAWN_PER_LANE),
        collapse: collapseLane,
      },
    }]
  }), [collapseLane, drawTasks, nodesByLane, selectedDeck, stackedLanes])
  const laneMinHeights = useMemo(() => Object.fromEntries(TASK_FLOW_LANES.map(({ key }) => [
    key,
    taskFlowLaneHeight(displayedTaskNodes
      .filter(node => node.data.item.lane === key)
      .map(node => ({ lane: key, position: node.position }))),
  ])) as Record<TaskFlowLane, number>, [displayedTaskNodes])
  const resizeLane = useCallback((
    lane: TaskFlowLane,
    width: number,
    height: number,
    finished: boolean,
  ) => {
    setLaneSizes(current => {
      const next = {
        ...current,
        [lane]: {
          width: Math.max(LANE_MIN_WIDTH, Math.round(width)),
          height: Math.max(LANE_MIN_HEIGHT, laneMinHeights[lane], Math.round(height)),
        },
      }
      if (finished) {
        saveLayout(
          layoutContext,
          nodesRef.current,
          edgesRef.current,
          getViewport(),
          next,
          drawnByLaneRef.current,
        )
      }
      return next
    })
  }, [getViewport, laneMinHeights, layoutContext])
  const laneNodes = useMemo<LaneNode[]>(() => {
    let x = 0
    return laneCounts.map(lane => {
      const size = laneSizes[lane.key]
      const node: LaneNode = {
        id: taskFlowLaneNodeId(lane.key), type: 'lane',
        position: { x, y: LANE_Y },
        className: resizeMode ? 'task-flow-lane--resizing' : 'task-flow-lane--passive',
        draggable: false, selectable: false, connectable: false, deletable: false, zIndex: 0,
        style: { width: size.width, height: size.height },
        data: {
          lane: lane.key,
          label: lane.label,
          description: lane.description,
          count: lane.count,
          resizeEnabled: resizeMode,
          minHeight: laneMinHeights[lane.key],
          resizeLane,
        },
      }
      x += size.width + LANE_GAP
      return node
    })
  }, [laneCounts, laneMinHeights, laneSizes, resizeLane, resizeMode])
  const workflowNodes = useMemo<WorkflowNode[]>(() => {
    const definitions = view === 'control'
      ? [
          ['entry', '待办进入', '研判、照片和下发任务汇入', 'blue'],
          ['work', '审核与处理', '研判、调照片、审核去向', 'amber'],
          ['publish', '发布或回传', '发布下发数据或返回核查人', 'blue'],
          ['done', '完成', '任务离开当前待办', 'green'],
        ]
      : [
          ['entry', '收到任务', '组长分配后进入个人任务流', 'blue'],
          ['work', '核查处理', '核实人员、地址和核查结果', 'amber'],
          ['wait', '协作等待', '等待研判或照片等外部结果', 'red'],
          ['done', '完成或回流', '完成核查，或协作后继续处理', 'green'],
        ]
    return definitions.map(([key, label, description, tone], index) => ({
      id: `task-flow-workflow-${key}`,
      type: 'workflow',
      position: { x: index * 300, y: 0 },
      draggable: false,
      selectable: false,
      deletable: false,
      data: { label, description, tone: tone as WorkflowNodeData['tone'] },
    }))
  }, [view])
  const systemEdges = useMemo<Edge[]>(() => {
    const ordered = workflowNodes.map(node => node.id)
    const result: Edge[] = ordered.slice(0, -1).map((source, index) => ({
      id: `task-flow-system-${source}-${ordered[index + 1]}`,
      source,
      target: ordered[index + 1],
      type: 'smoothstep',
      markerEnd: { type: MarkerType.ArrowClosed },
      selectable: false,
      deletable: false,
      animated: index === 0,
      style: { strokeWidth: 2 },
    }))
    if (view === 'person') {
      result.push({
        id: 'task-flow-system-return-to-work',
        source: 'task-flow-workflow-wait',
        target: 'task-flow-workflow-work',
        type: 'smoothstep',
        label: '协作完成后继续',
        selectable: false,
        deletable: false,
        style: { strokeDasharray: '4 4', strokeWidth: 1.6 },
      })
    }
    return result
  }, [view, workflowNodes])
  const taskAttachmentEdges = useMemo<Edge[]>(() => {
    const anchorForLane = (lane: TaskFlowLane) => view === 'control'
      ? lane === 'exception' ? 'task-flow-workflow-publish' : 'task-flow-workflow-work'
      : lane === 'waiting' ? 'task-flow-workflow-wait' : 'task-flow-workflow-work'
    const deckLaneSet = new Set(deckNodes.map(node => node.data.lane))
    const deckConnections = deckNodes.flatMap(deck => {
      const lane = deck.data.lane
      const taskEdges = displayedTaskNodes
        .filter(node => node.data.item.lane === lane)
        .map(node => ({
          id: `task-flow-deck-edge-${deck.id}-${node.id}`,
          source: deck.id,
          target: node.id,
          type: 'smoothstep',
          markerEnd: { type: MarkerType.ArrowClosed },
          selectable: false,
          deletable: false,
          style: { strokeWidth: 1.8 },
        }))
      return [{
        id: `task-flow-workflow-deck-${deck.id}`,
        source: anchorForLane(lane),
        target: deck.id,
        type: 'smoothstep',
        selectable: false,
        deletable: false,
        style: { strokeWidth: 1.5, strokeDasharray: '5 4' },
      }, ...taskEdges]
    })
    const looseTaskConnections = displayedTaskNodes
      .filter(node => !deckLaneSet.has(node.data.item.lane))
      .map(node => ({
        id: `task-flow-workflow-task-${node.id}`,
        source: anchorForLane(node.data.item.lane),
        target: node.id,
        type: 'smoothstep',
        markerEnd: { type: MarkerType.ArrowClosed },
        selectable: false,
        deletable: false,
        style: { strokeWidth: 1.3, strokeDasharray: '5 4' },
      }))
    return [...deckConnections, ...looseTaskConnections]
  }, [deckNodes, displayedTaskNodes, view])
  const displayedIds = useMemo(() => new Set([
    ...displayedTaskNodes.map(node => node.id),
    ...deckNodes.map(node => node.id),
  ]), [deckNodes, displayedTaskNodes])
  const visibleManualEdges = useMemo(() => edges.filter(edge => (
    displayedIds.has(edge.source) && displayedIds.has(edge.target)
  )), [displayedIds, edges])
  const flowNodes = useMemo<FlowNode[]>(() => [
    ...workflowNodes,
    ...laneNodes,
    ...deckNodes,
    ...displayedTaskNodes,
  ], [deckNodes, displayedTaskNodes, laneNodes, workflowNodes])
  const flowEdges = useMemo(() => [
    ...systemEdges,
    ...taskAttachmentEdges,
    ...visibleManualEdges,
  ], [systemEdges, taskAttachmentEdges, visibleManualEdges])

  const onNodesChange = useCallback((changes: NodeChange<FlowNode>[]) => {
    const taskChanges = changes.filter(change => !change.id.startsWith('task-flow-lane-')) as NodeChange<TaskNode>[]
    if (taskChanges.length) setNodes(current => applyNodeChanges(taskChanges, current))
  }, [])
  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    setEdges(current => {
      const next = applyEdgeChanges(changes, current)
      saveLayout(
        layoutContext, nodes, next, getViewport(),
        laneSizesRef.current, drawnByLaneRef.current,
      )
      return next
    })
  }, [getViewport, layoutContext, nodes])
  const onConnect = useCallback((connection: Connection) => {
    setEdges(current => {
      const next = addEdge({ ...connection, type: 'smoothstep', markerEnd: { type: MarkerType.ArrowClosed }, style: { strokeDasharray: '6 5' } }, current)
      saveLayout(
        layoutContext, nodes, next, getViewport(),
        laneSizesRef.current, drawnByLaneRef.current,
      )
      return next
    })
  }, [getViewport, layoutContext, nodes])
  const resetLayout = () => {
    const laneIndexes: Record<TaskFlowLane, number> = { ready: 0, waiting: 0, exception: 0 }
    const next = nodes.map(node => {
      const lane = node.data.item.lane
      const drawnIndex = drawnByLane[lane].indexOf(node.id)
      if (stackedLanes.has(lane) && drawnIndex < 0) return node
      const position = stackedLanes.has(lane)
        ? { x: 20, y: 260 + drawnIndex * 196 }
        : defaultTaskFlowPosition(lane, laneIndexes[lane])
      if (!stackedLanes.has(lane)) laneIndexes[lane] += 1
      return { ...node, position }
    })
    const nextSizes = Object.fromEntries(TASK_FLOW_LANES.map(({ key }) => {
      const displayed = next.filter(node => (
        node.data.item.lane === key
        && (!stackedLanes.has(key) || drawnByLane[key].includes(node.id))
      ))
      return [key, {
        width: laneSizesRef.current[key].width,
        height: taskFlowLaneHeight(displayed.map(node => ({ lane: key, position: node.position }))),
      }]
    })) as LaneSizes
    setNodes(next)
    setLaneSizes(nextSizes)
    saveLayout(
      layoutContext, next, edges, getViewport(),
      nextSizes, drawnByLaneRef.current,
    )
    window.setTimeout(() => fitView({ padding: 0.08, duration: 350 }), 50)
  }
  const toggleResizeMode = () => {
    setResizeMode(current => {
      if (current) {
        saveLayout(
          layoutContext,
          nodesRef.current,
          edgesRef.current,
          getViewport(),
          laneSizesRef.current,
          drawnByLaneRef.current,
        )
      }
      return !current
    })
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
                    laneSizesRef.current, drawnByLaneRef.current,
                  )
                }}
                onMoveEnd={(_, viewport) => saveLayout(
                  layoutContext,
                  nodesRef.current,
                  edgesRef.current,
                  viewport,
                  laneSizesRef.current,
                  drawnByLaneRef.current,
                )}
                onNodeDoubleClick={(_, node) => {
                  if (node.type === 'task') openTask(node.data.item.openPath)
                  if (node.type === 'deck') drawTasks(node.data.lane, 1)
                }}
                nodesDeletable={false} autoPanOnNodeDrag={false} minZoom={0.2} maxZoom={1.6}
                deleteKeyCode={['Backspace', 'Delete']}
                proOptions={{ hideAttribution: true }}>
                <FlowPanel position="top-left" className="task-flow-canvas-toolbar">
                  <Button size="small" icon={<ReloadOutlined />} loading={loadingTasks}
                    onClick={() => void refreshTasks(false)}>刷新</Button>
                  <Button size="small" icon={<PartitionOutlined />}
                    onClick={() => void fitView({ padding: 0.08, duration: 350 })}>流程视图</Button>
                  <Button size="small" icon={<CompressOutlined />} onClick={resetLayout}>自动布局</Button>
                  <Button size="small" type={resizeMode ? 'primary' : 'default'} onClick={toggleResizeMode}>
                    {resizeMode ? '完成调整' : '调整区域'}
                  </Button>
                  <Button size="small" icon={<AppstoreOutlined />} onClick={collapseAllDecks}>全部收起</Button>
                </FlowPanel>
                <Background gap={24} size={1.2} />
                <MiniMap pannable zoomable nodeColor={node => {
                  if (node.type === 'lane') return 'rgba(148, 163, 184, 0.16)'
                  if (node.type === 'workflow') return '#64748b'
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
