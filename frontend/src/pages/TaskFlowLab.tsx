import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
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
  ExperimentOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import { Alert, Button, Empty, Input, Select, Skeleton, Tag, message } from 'antd'
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  getMobileTaskFilterOptions,
  listMobileTasks,
  type MobileTaskFilterOption,
  type MobileTaskItem,
} from '../api/client'
import useDebouncedValue from '../hooks/useDebouncedValue'
import useMobileViewport from '../hooks/useMobileViewport'
import { MOBILE_TASK_TYPES } from '../utils/mobileTaskRouting'
import {
  TASK_FLOW_LANES,
  defaultTaskFlowPosition,
  mergeTaskFlowInspectors,
  taskFlowLane,
  taskFlowNodeId,
  type TaskFlowLane,
} from '../utils/taskFlow'

const AUTO_REFRESH_MS = 30_000
const MAX_TASKS_PER_TYPE = 100
const LAST_INSPECTOR_KEY = 'binhu-task-flow-lab:last-inspector'
const LAYOUT_KEY_PREFIX = 'binhu-task-flow-lab:layout:'

const STATE_META = {
  unchecked: { label: '未核查', color: 'gold' },
  checked: { label: '待补结果', color: 'orange' },
  completed: { label: '已完成', color: 'green' },
} as const

type TaskFlowNodeData = {
  task: MobileTaskItem
  lane: TaskFlowLane
  fresh: boolean
  openTask: (task: MobileTaskItem) => void
} & Record<string, unknown>

type TaskNode = Node<TaskFlowNodeData, 'task'>

interface SavedTaskFlowLayout {
  positions: Record<string, { x: number; y: number }>
  edges: Edge[]
  viewport: Viewport | null
}

function layoutStorageKey(inspector: string) {
  return `${LAYOUT_KEY_PREFIX}${encodeURIComponent(inspector)}`
}

function readSavedLayout(inspector: string): SavedTaskFlowLayout {
  try {
    const parsed = JSON.parse(localStorage.getItem(layoutStorageKey(inspector)) || '{}')
    return {
      positions: parsed?.positions && typeof parsed.positions === 'object' ? parsed.positions : {},
      edges: Array.isArray(parsed?.edges) ? parsed.edges : [],
      viewport: parsed?.viewport
        && Number.isFinite(parsed.viewport.x)
        && Number.isFinite(parsed.viewport.y)
        && Number.isFinite(parsed.viewport.zoom)
        ? parsed.viewport
        : null,
    }
  } catch {
    return { positions: {}, edges: [], viewport: null }
  }
}

function saveLayout(
  inspector: string,
  nodes: TaskNode[],
  edges: Edge[],
  viewport: Viewport | null = null,
) {
  if (!inspector) return
  const positions = Object.fromEntries(nodes.map(node => [node.id, node.position]))
  localStorage.setItem(layoutStorageKey(inspector), JSON.stringify({ positions, edges, viewport }))
}

function TaskCardNode({ data, selected }: NodeProps<TaskNode>) {
  const { task, lane, fresh, openTask } = data
  const state = STATE_META[task.state]
  return (
    <article
      className={`task-flow-node task-flow-node--${lane}${selected ? ' is-selected' : ''}${fresh ? ' is-fresh' : ''}`}
    >
      <Handle type="target" position={Position.Left} className="task-flow-node__handle" />
      <div className="task-flow-node__header">
        <div className="min-w-0">
          <div className="task-flow-node__type">{task.parser_type}</div>
          <div className="task-flow-node__title" title={task.summary.title || '未填写姓名'}>
            {task.summary.title || '未填写姓名'}
          </div>
        </div>
        <Tag color={state.color} className="m-0 shrink-0">{state.label}</Tag>
      </div>
      <div className="task-flow-node__body">
        {task.community && <span>{task.community}</span>}
        {task.summary.deadline && <span>截止 {task.summary.deadline}</span>}
        {task.summary.address && <span className="task-flow-node__address">{task.summary.address}</span>}
      </div>
      <div className="task-flow-node__footer">
        <span>{task.inspector || '待分配'}</span>
        <button
          type="button"
          className="task-flow-node__open nodrag nopan"
          onClick={event => {
            event.stopPropagation()
            openTask(task)
          }}
        >
          打开详情
        </button>
      </div>
      <Handle type="source" position={Position.Right} className="task-flow-node__handle" />
    </article>
  )
}

const MemoTaskCardNode = memo(TaskCardNode)
const NODE_TYPES = { task: MemoTaskCardNode }

async function loadTasksForInspector(inspector: string, passive: boolean) {
  const results = await Promise.all(MOBILE_TASK_TYPES.map(async parserType => {
    const first = await listMobileTasks({
      parser_type: parserType,
      scope: 'all',
      status: 'pending',
      inspectors: [inspector],
      sort: 'priority',
      page: 1,
      page_size: 50,
    }, { passive })
    const pageCount = Math.min(Math.ceil(first.total / 50), MAX_TASKS_PER_TYPE / 50)
    const remaining = pageCount > 1
      ? await Promise.all(Array.from({ length: pageCount - 1 }, (_, index) => (
          listMobileTasks({
            parser_type: parserType,
            scope: 'all',
            status: 'pending',
            inspectors: [inspector],
            sort: 'priority',
            page: index + 2,
            page_size: 50,
          }, { passive })
        )))
      : []
    return {
      tasks: [first, ...remaining].flatMap(page => page.data),
      total: first.total,
    }
  }))
  return {
    tasks: results.flatMap(result => result.tasks),
    total: results.reduce((sum, result) => sum + result.total, 0),
  }
}

function TaskFlowLabContent() {
  const navigate = useNavigate()
  const mobile = useMobileViewport()
  const { fitView, getViewport, setViewport } = useReactFlow<TaskNode>()
  const [inspectors, setInspectors] = useState<MobileTaskFilterOption[]>([])
  const [selectedInspector, setSelectedInspector] = useState(
    () => localStorage.getItem(LAST_INSPECTOR_KEY) || '',
  )
  const [keyword, setKeyword] = useState('')
  const debouncedKeyword = useDebouncedValue(keyword, 300)
  const [nodes, setNodes] = useState<TaskNode[]>([])
  const [edges, setEdges] = useState<Edge[]>([])
  const [availableTotal, setAvailableTotal] = useState(0)
  const [loadingInspectors, setLoadingInspectors] = useState(true)
  const [loadingTasks, setLoadingTasks] = useState(false)
  const [error, setError] = useState('')
  const requestId = useRef(0)
  const knownNodeIds = useRef<Set<string> | null>(null)
  const fittedInspector = useRef('')
  const freshTimer = useRef<number | null>(null)
  const nodesRef = useRef<TaskNode[]>([])

  useEffect(() => {
    nodesRef.current = nodes
  }, [nodes])

  const openTask = useCallback((task: MobileTaskItem) => {
    navigate(`/tasks/${encodeURIComponent(task.parser_type)}/${encodeURIComponent(task.row_key)}?scope=all`)
  }, [navigate])

  const loadInspectors = useCallback(async () => {
    setLoadingInspectors(true)
    try {
      const results = await Promise.all(MOBILE_TASK_TYPES.map(parserType => (
        getMobileTaskFilterOptions(parserType, 'all')
      )))
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

  useEffect(() => {
    void loadInspectors()
  }, [loadInspectors])

  const refreshTasks = useCallback(async (passive = false) => {
    if (!selectedInspector) {
      setNodes([])
      setEdges([])
      setAvailableTotal(0)
      return
    }
    const currentRequest = ++requestId.current
    if (!passive) setLoadingTasks(true)
    try {
      const result = await loadTasksForInspector(selectedInspector, passive)
      if (currentRequest !== requestId.current) return
      const saved = readSavedLayout(selectedInspector)
      const previousPositions = new Map(nodesRef.current.map(node => [node.id, node.position]))
      const laneIndexes: Record<TaskFlowLane, number> = { ready: 0, waiting: 0, exception: 0 }
      const currentIds = new Set(result.tasks.map(taskFlowNodeId))
      const newIds = knownNodeIds.current
        ? new Set([...currentIds].filter(id => !knownNodeIds.current?.has(id)))
        : new Set<string>()
      const nextNodes = result.tasks.map<TaskNode>(task => {
        const id = taskFlowNodeId(task)
        const lane = taskFlowLane(task)
        const position = previousPositions.get(id)
          || saved.positions[id]
          || defaultTaskFlowPosition(lane, laneIndexes[lane])
        laneIndexes[lane] += 1
        return {
          id,
          type: 'task',
          position,
          data: { task, lane, fresh: newIds.has(id), openTask },
        }
      })
      const nextEdges = saved.edges.filter(edge => currentIds.has(edge.source) && currentIds.has(edge.target))
      knownNodeIds.current = currentIds
      setNodes(nextNodes)
      setEdges(nextEdges)
      setAvailableTotal(result.total)
      setError('')
      if (newIds.size > 0) {
        message.info(`${selectedInspector}新增 ${newIds.size} 条待办任务`)
        if (freshTimer.current) window.clearTimeout(freshTimer.current)
        freshTimer.current = window.setTimeout(() => {
          setNodes(current => current.map(node => ({
            ...node,
            data: { ...node.data, fresh: false },
          })))
        }, 8000)
      }
      if (fittedInspector.current !== selectedInspector && nextNodes.length > 0) {
        fittedInspector.current = selectedInspector
        window.setTimeout(() => {
          if (saved.viewport) void setViewport(saved.viewport, { duration: 0 })
          else void fitView({ padding: 0.16, duration: 350 })
        }, 50)
      }
    } catch (reason: any) {
      if (currentRequest !== requestId.current) return
      setError(reason?.response?.data?.detail || reason?.message || '任务流读取失败')
    } finally {
      if (currentRequest === requestId.current) setLoadingTasks(false)
    }
  }, [fitView, openTask, selectedInspector, setViewport])

  useEffect(() => {
    knownNodeIds.current = null
    fittedInspector.current = ''
    if (selectedInspector) localStorage.setItem(LAST_INSPECTOR_KEY, selectedInspector)
    void refreshTasks(false)
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refreshTasks(true)
    }, AUTO_REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [refreshTasks, selectedInspector])

  useEffect(() => () => {
    if (freshTimer.current) window.clearTimeout(freshTimer.current)
  }, [])

  const visibleNodes = useMemo(() => {
    const normalized = debouncedKeyword.trim().toLowerCase()
    if (!normalized) return nodes
    return nodes.filter(node => {
      const task = node.data.task
      return [
        task.parser_type,
        task.summary.title,
        task.community,
        task.inspector,
        task.summary.address,
      ].some(value => String(value || '').toLowerCase().includes(normalized))
    })
  }, [debouncedKeyword, nodes])

  const visibleIds = useMemo(() => new Set(visibleNodes.map(node => node.id)), [visibleNodes])
  const visibleEdges = useMemo(
    () => edges.filter(edge => visibleIds.has(edge.source) && visibleIds.has(edge.target)),
    [edges, visibleIds],
  )
  const laneCounts = useMemo(() => TASK_FLOW_LANES.map(lane => ({
    ...lane,
    count: visibleNodes.filter(node => node.data.lane === lane.key).length,
  })), [visibleNodes])

  const onNodesChange = useCallback((changes: NodeChange<TaskNode>[]) => {
    setNodes(current => applyNodeChanges(changes, current))
  }, [])

  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    setEdges(current => {
      const next = applyEdgeChanges(changes, current)
      saveLayout(selectedInspector, nodes, next, getViewport())
      return next
    })
  }, [getViewport, nodes, selectedInspector])

  const onConnect = useCallback((connection: Connection) => {
    setEdges(current => {
      const next = addEdge({
        ...connection,
        type: 'smoothstep',
        markerEnd: { type: MarkerType.ArrowClosed },
        style: { strokeDasharray: '6 5' },
      }, current)
      saveLayout(selectedInspector, nodes, next, getViewport())
      return next
    })
  }, [getViewport, nodes, selectedInspector])

  const resetLayout = () => {
    const laneIndexes: Record<TaskFlowLane, number> = { ready: 0, waiting: 0, exception: 0 }
    const next = nodes.map(node => {
      const lane = node.data.lane
      const position = defaultTaskFlowPosition(lane, laneIndexes[lane])
      laneIndexes[lane] += 1
      return { ...node, position }
    })
    setNodes(next)
    saveLayout(selectedInspector, next, edges, getViewport())
    window.setTimeout(() => fitView({ padding: 0.16, duration: 350 }), 50)
  }

  return (
    <div className="task-flow-lab mx-auto max-w-[1680px] space-y-4 pb-4">
      <section className="app-card task-flow-lab__hero">
        <div>
          <div className="task-flow-lab__eyebrow"><ExperimentOutlined /> 超级管理员内测</div>
          <h1>我的任务流</h1>
          <p>选择一名核查人，平台会把其现有流口待办自动投放到沙盒。虚线连线和节点位置只属于当前浏览器，不会改变真实业务流程。</p>
        </div>
        <div className="task-flow-lab__hero-actions">
          <Button icon={<ReloadOutlined />} loading={loadingTasks} disabled={!selectedInspector} onClick={() => void refreshTasks(false)}>
            刷新任务
          </Button>
          <Button disabled={!nodes.length} onClick={resetLayout}>自动整理</Button>
        </div>
      </section>

      <section className="app-card task-flow-lab__toolbar">
        <Select
          showSearch
          loading={loadingInspectors}
          value={selectedInspector || undefined}
          placeholder="选择核查人开始内测"
          optionFilterProp="label"
          className="task-flow-lab__inspector"
          options={inspectors.map(option => ({
            value: option.value,
            label: `${option.label} · ${option.count}条当前记录`,
          }))}
          onChange={setSelectedInspector}
        />
        <Input
          allowClear
          prefix={<SearchOutlined />}
          value={keyword}
          placeholder="筛选当前沙盒中的任务"
          onChange={event => setKeyword(event.target.value)}
        />
        <div className="task-flow-lab__summary">
          {laneCounts.map(lane => (
            <span key={lane.key} className={`task-flow-lab__summary-item is-${lane.key}`} title={lane.description}>
              {lane.label}<strong>{lane.count}</strong>
            </span>
          ))}
        </div>
      </section>

      {error && <Alert type="error" showIcon message={error} />}
      {availableTotal > nodes.length && (
        <Alert
          type="warning"
          showIcon
          message={`当前共有 ${availableTotal} 条待办，内测画布最多读取每类 ${MAX_TASKS_PER_TYPE} 条；请结合搜索或进入原任务列表处理其余数据。`}
        />
      )}

      {!selectedInspector ? (
        <section className="app-card task-flow-lab__empty">
          {loadingInspectors ? <Skeleton active paragraph={{ rows: 4 }} /> : (
            <Empty description="先选择一名核查人，查看任务如何自动进入个人沙盒" />
          )}
        </section>
      ) : mobile ? (
        <section className="task-flow-lab__mobile-list">
          {visibleNodes.length ? visibleNodes.map(node => (
            <button key={node.id} type="button" className={`app-card task-flow-mobile-card is-${node.data.lane}`} onClick={() => openTask(node.data.task)}>
              <span className="task-flow-mobile-card__type">{node.data.task.parser_type}</span>
              <strong>{node.data.task.summary.title || '未填写姓名'}</strong>
              <span>{node.data.task.community || '未填写社区'} · {node.data.task.summary.deadline || '未填写截止日期'}</span>
            </button>
          )) : <div className="app-card p-8"><Empty description="当前筛选下没有待办" /></div>}
        </section>
      ) : (
        <section className="app-card task-flow-lab__canvas-shell">
          <div className="task-flow-lab__lane-legend">
            {laneCounts.map(lane => (
              <div key={lane.key} className={`task-flow-lab__lane is-${lane.key}`}>
                <strong>{lane.label}</strong>
                <span>{lane.description}</span>
              </div>
            ))}
          </div>
          <div className="task-flow-lab__canvas">
            {loadingTasks && !nodes.length ? <div className="task-flow-lab__loading"><Skeleton active paragraph={{ rows: 6 }} /></div> : (
              <ReactFlow<TaskNode>
                nodes={visibleNodes}
                edges={visibleEdges}
                nodeTypes={NODE_TYPES}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onConnect={onConnect}
                onNodeDragStop={(_, node) => {
                  const next = nodes.map(item => item.id === node.id ? { ...item, position: node.position } : item)
                  setNodes(next)
                  saveLayout(selectedInspector, next, edges, getViewport())
                }}
                onMoveEnd={(_, viewport) => saveLayout(selectedInspector, nodesRef.current, edges, viewport)}
                onNodeDoubleClick={(_, node) => openTask(node.data.task)}
                nodesDeletable={false}
                minZoom={0.25}
                maxZoom={1.6}
                deleteKeyCode={['Backspace', 'Delete']}
                proOptions={{ hideAttribution: true }}
              >
                <Background gap={24} size={1.2} />
                <MiniMap pannable zoomable nodeColor={node => {
                  const lane = (node.data as TaskFlowNodeData).lane
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
  return (
    <ReactFlowProvider>
      <TaskFlowLabContent />
    </ReactFlowProvider>
  )
}
