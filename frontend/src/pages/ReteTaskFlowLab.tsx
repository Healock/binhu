import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { createRoot } from 'react-dom/client'
import { AimOutlined, ExperimentOutlined, HistoryOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Empty, Input, Modal, Segmented, Select, Skeleton, Tag, message } from 'antd'
import { useNavigate } from 'react-router-dom'
import { ClassicPreset, GetSchemes, NodeEditor } from 'rete'
import { AreaExtensions, AreaPlugin } from 'rete-area-plugin'
import { AutoArrangePlugin, Presets as ArrangePresets } from 'rete-auto-arrange-plugin'
import { ReactPlugin, Presets as ReactPresets, useRete, type ReactArea2D, type RenderEmit } from 'rete-react-plugin'
import {
  getTaskGraphConfig,
  getTaskGraphOptions,
  previewTaskGraphBackfill,
  runTaskGraphBackfill,
  searchTaskGraph,
  updateTaskGraphConfig,
  type MobileTaskFilterOption,
  type TaskGraphEdge,
  type TaskGraphNode,
  type TaskGraphPreview,
} from '../api/client'
import useDebouncedValue from '../hooks/useDebouncedValue'
import useMobileViewport from '../hooks/useMobileViewport'

const LAYOUT_KEY = 'binhu-rete-task-graph-layout-v4:'
const PAGE_SIZE = 20

const STATUS_META: Record<string, { label: string; color: string }> = {
  ready: { label: '可处理', color: 'blue' },
  blocked: { label: '等待前置', color: 'orange' },
  in_progress: { label: '处理中', color: 'cyan' },
  completed: { label: '已完成', color: 'green' },
  cancelled: { label: '已取消', color: 'default' },
  source_missing: { label: '来源失效', color: 'red' },
  archived: { label: '已归档', color: 'default' },
}

class TaskInstanceNode extends ClassicPreset.Node {
  task: TaskGraphNode
  width = 292
  height = 214
  open: () => void

  constructor(task: TaskGraphNode, socket: ClassicPreset.Socket, open: () => void) {
    super(task.title)
    this.task = task
    this.open = open
    this.addInput('in', new ClassicPreset.Input(socket, '前置', true))
    this.addOutput('next', new ClassicPreset.Output(socket, '后置', true))
  }
}

class TaskDependencyConnection extends ClassicPreset.Connection<TaskInstanceNode, TaskInstanceNode> {
  graphEdge: TaskGraphEdge
  isLoop?: boolean

  constructor(source: TaskInstanceNode, target: TaskInstanceNode, edge: TaskGraphEdge) {
    super(source, 'next', target, 'in')
    this.graphEdge = edge
  }
}

type Schemes = GetSchemes<TaskInstanceNode, TaskDependencyConnection>
type AreaExtra = ReactArea2D<Schemes>
type TaskArea = AreaPlugin<Schemes, AreaExtra>
type TaskAreaHandle = TaskArea & {
  editorInstance: NodeEditor<Schemes>
  arrangePlugin: AutoArrangePlugin<Schemes, AreaExtra>
  contextKey: string
  currentGraphNodes: TaskGraphNode[]
  currentGraphEdges: TaskGraphEdge[]
  suspendPositionSave: boolean
  setGraph: (nodes: TaskGraphNode[], edges: TaskGraphEdge[], autoArrange?: boolean) => Promise<void>
  arrangeGraph: () => Promise<void>
}

function savedPositions(contextKey: string): Record<string, { x: number; y: number }> {
  try {
    const raw = JSON.parse(localStorage.getItem(`${LAYOUT_KEY}${encodeURIComponent(contextKey)}`) || '{}')
    return raw && typeof raw === 'object' ? raw : {}
  } catch {
    return {}
  }
}

function savePosition(contextKey: string, nodeId: string, position: { x: number; y: number }) {
  if (!contextKey) return
  const positions = savedPositions(contextKey)
  positions[nodeId] = position
  localStorage.setItem(`${LAYOUT_KEY}${encodeURIComponent(contextKey)}`, JSON.stringify(positions))
}

async function layoutIndependentNodes(
  area: TaskArea,
  editor: NodeEditor<Schemes>,
  graphNodes: TaskGraphNode[],
  graphEdges: TaskGraphEdge[],
  contextKey: string,
  saved: Record<string, { x: number; y: number }>,
) {
  const waitForNodeViews = async () => {
    for (let attempt = 0; attempt < 12; attempt += 1) {
      if (independent.every(task => area.nodeViews.has(task.id))) return
      await new Promise(resolve => window.setTimeout(resolve, 40))
    }
  }
  const connected = new Set(graphEdges.flatMap(edge => [edge.source, edge.target]))
  const independent = graphNodes
    .filter(task => !connected.has(task.id))
    .sort((left, right) => left.category.localeCompare(right.category, 'zh-CN') || left.title.localeCompare(right.title, 'zh-CN'))
  await waitForNodeViews()
  const columns = Math.max(1, Math.ceil(Math.sqrt(independent.length)))
  const columnGap = 330
  const rowGap = 250
  const connectedBottom = graphNodes
    .filter(task => connected.has(task.id))
    .reduce((bottom, task) => {
      const view = area.nodeViews.get(task.id)
      const position = saved[task.id] || view?.position
      return position ? Math.max(bottom, position.y + 214) : bottom
    }, 0)
  const gridStartY = connectedBottom > 0 ? connectedBottom + 120 : 80
  for (const [slot, task] of independent.entries()) {
    if (saved[task.id]) continue
    const node = editor.getNode(task.id)
    if (!node) continue
    const column = slot % columns
    const row = Math.floor(slot / columns)
    const nextPosition = {
      x: 80 + column * columnGap,
      y: gridStartY + row * rowGap,
    }
    await area.translate(node.id, nextPosition)
    const view = area.nodeViews.get(node.id)
    if (!view || Math.abs(view.position.x - nextPosition.x) > 1 || Math.abs(view.position.y - nextPosition.y) > 1) {
      await new Promise(resolve => window.setTimeout(resolve, 0))
      await area.translate(node.id, nextPosition)
    }
    // Rete may emit a programmatic translation after the guard is cleared;
    // write the intended grid coordinate explicitly instead of trusting the
    // transient event order.
    savePosition(contextKey, node.id, nextPosition)
  }
}

function TaskNodeCard({ data, emit }: { data: Schemes['Node']; emit: RenderEmit<Schemes> }) {
  const node = data as TaskInstanceNode
  const task = node.task
  const status = STATUS_META[task.status] || { label: task.status, color: 'default' }
  const buttonLabel = task.access_mode === 'readonly' ? '只读查看' : task.access_mode === 'blocked' ? '查看等待' : '进入处理'
  const accent = task.task_type === 'analysis' ? '#8b5cf6' : task.status === 'blocked' ? '#d97706' : '#2563eb'
  return (
    <article className={`rete-task-node rete-task-instance is-${task.access_mode} is-${task.status}`} style={{ '--rete-node-accent': accent } as CSSProperties}>
      {Object.entries(node.inputs).map(([key, input]) => input && <div key={key} className="rete-task-node__socket rete-task-node__socket--input"><ReactPresets.classic.RefSocket<Schemes> name="input-socket" emit={emit} nodeId={node.id} side="input" socketKey={key} payload={input.socket} /></div>)}
      {Object.entries(node.outputs).map(([key, output]) => output && <div key={key} className="rete-task-node__socket rete-task-node__socket--output"><ReactPresets.classic.RefSocket<Schemes> name="output-socket" emit={emit} nodeId={node.id} side="output" socketKey={key} payload={output.socket} /></div>)}
      <div className="rete-task-node__topline"><span className="rete-task-node__kind">{task.category}</span><span className="flex gap-1">{task.sync_warning && <Tag color="red">同步待确认</Tag>}<Tag color={status.color}>{status.label}</Tag></span></div>
      <strong title={task.title}>{task.title}</strong>
      <div className="rete-task-instance__meta">{task.community && <span>{task.community}</span>}<span>{task.owner || '待分配'}</span><span>{task.relationship === 'predecessor' ? '我的前置任务' : task.relationship === 'successor' ? '我的后置任务' : '我负责的任务'}</span></div>
      <p>{task.description}</p>
      <button type="button" className="rete-task-node__open" onPointerDown={event => event.stopPropagation()} onClick={node.open}>{buttonLabel}</button>
    </article>
  )
}

async function createEditor(container: HTMLElement, navigate: (path: string) => void): Promise<TaskAreaHandle> {
  const editor = new NodeEditor<Schemes>()
  const area = new AreaPlugin<Schemes, AreaExtra>(container)
  editor.use(area)
  const render = new ReactPlugin<Schemes, AreaExtra>({ createRoot })
  render.addPreset(ReactPresets.classic.setup({ customize: { node: () => TaskNodeCard } }))
  area.use(render)
  const arrange = new AutoArrangePlugin<Schemes, AreaExtra>()
  arrange.addPreset(ArrangePresets.classic.setup({ spacing: 96, top: 38, bottom: 38 }))
  area.use(arrange)
  AreaExtensions.simpleNodesOrder(area)
  const handle = area as TaskAreaHandle
  handle.editorInstance = editor
  handle.arrangePlugin = arrange
  handle.contextKey = ''
  handle.currentGraphNodes = []
  handle.currentGraphEdges = []
  handle.suspendPositionSave = false
  handle.arrangeGraph = async () => {
    localStorage.removeItem(`${LAYOUT_KEY}${encodeURIComponent(handle.contextKey)}`)
    handle.suspendPositionSave = true
    try {
      await arrange.layout({ options: { 'elk.algorithm': 'layered', 'elk.direction': 'RIGHT', 'elk.spacing.nodeNode': '84', 'elk.layered.spacing.nodeNodeBetweenLayers': '110' } })
      await layoutIndependentNodes(area, editor, handle.currentGraphNodes, handle.currentGraphEdges, handle.contextKey, {})
      await AreaExtensions.zoomAt(area, editor.getNodes(), { scale: 0.82 })
    } finally {
      handle.suspendPositionSave = false
    }
  }
  handle.setGraph = async (graphNodes, graphEdges, autoArrange = false) => {
    handle.currentGraphNodes = graphNodes
    handle.currentGraphEdges = graphEdges
    await editor.clear()
    const socket = new ClassicPreset.Socket('task-dependency')
    const nodes = new Map<string, TaskInstanceNode>()
    for (const task of graphNodes) {
      const node = new TaskInstanceNode(task, socket, () => navigate(task.open_path))
      node.id = task.id
      nodes.set(task.id, node)
      await editor.addNode(node)
    }
    for (const edge of graphEdges) {
      const source = nodes.get(edge.source)
      const target = nodes.get(edge.target)
      if (source && target) await editor.addConnection(new TaskDependencyConnection(source, target, edge))
    }
    const positions = autoArrange ? {} : savedPositions(handle.contextKey)
    handle.suspendPositionSave = true
    try {
      await arrange.layout({ options: { 'elk.algorithm': 'layered', 'elk.direction': 'RIGHT', 'elk.spacing.nodeNode': '84', 'elk.layered.spacing.nodeNodeBetweenLayers': '110' } })
      await layoutIndependentNodes(area, editor, graphNodes, graphEdges, handle.contextKey, positions)
      for (const node of editor.getNodes()) if (positions[node.id]) await area.translate(node.id, positions[node.id])
      if (nodes.size) await AreaExtensions.zoomAt(area, editor.getNodes(), { scale: 0.82 })
    } finally {
      handle.suspendPositionSave = false
    }
  }
  area.addPipe(context => {
    if (context && typeof context === 'object' && 'type' in context && context.type === 'nodetranslated') {
      const data = (context as any).data
      if (!handle.suspendPositionSave) savePosition(handle.contextKey, String(data.id), { x: Number(data.position.x), y: Number(data.position.y) })
    }
    return context
  })
  return handle
}

function mergeById<T extends { id: string }>(current: T[], incoming: T[]): T[] {
  const values = new Map(current.map(item => [item.id, item]))
  incoming.forEach(item => values.set(item.id, item))
  return [...values.values()]
}

export default function ReteTaskFlowLab() {
  const navigate = useNavigate()
  const mobile = useMobileViewport()
  const [areaRef, area] = useRete<TaskAreaHandle>(useCallback(container => createEditor(container, navigate), [navigate]))
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [options, setOptions] = useState<MobileTaskFilterOption[]>([])
  const [view, setView] = useState<'person' | 'queue'>('person')
  const [inspector, setInspector] = useState('')
  const [history, setHistory] = useState(false)
  const [taskType, setTaskType] = useState('all')
  const [keyword, setKeyword] = useState('')
  const debouncedKeyword = useDebouncedValue(keyword, 350)
  const [nodes, setNodes] = useState<TaskGraphNode[]>([])
  const [edges, setEdges] = useState<TaskGraphEdge[]>([])
  const [nextCursors, setNextCursors] = useState<Record<string, number>>({})
  const [loading, setLoading] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState('')
  const [preview, setPreview] = useState<TaskGraphPreview | null>(null)
  const [activating, setActivating] = useState(false)
  const requestId = useRef(0)
  const contextKey = `${view}:${view === 'person' ? inspector : '基础管控'}:${history ? 'history' : 'active'}:${taskType}`

  useEffect(() => { void Promise.all([getTaskGraphConfig(), getTaskGraphOptions()]).then(([config, graphOptions]) => { setEnabled(config.enabled); setOptions(graphOptions.inspectors); setInspector(current => current || graphOptions.inspectors[0]?.value || '') }).catch(reason => setError(reason?.response?.data?.detail || reason?.message || '任务图配置读取失败')) }, [])

  const loadGraph = useCallback(async (append = false, passive = false) => {
    if (!enabled || (view === 'person' && !inspector)) { setNodes([]); setEdges([]); setNextCursors({}); return }
    const current = ++requestId.current
    append ? setLoadingMore(true) : setLoading(true)
    if (!passive) setError('')
    try {
      const result = await searchTaskGraph({ view, person_user_id: view === 'person' ? Number(inspector) : undefined, queue: view === 'queue' ? '基础管控' : undefined, history, task_types: taskType === 'all' ? [] : [taskType], keyword: debouncedKeyword, cursors: append ? nextCursors : {}, page_size: PAGE_SIZE }, { passive })
      if (current !== requestId.current) return
      setNodes(existing => append ? mergeById(existing, result.nodes) : result.nodes)
      setEdges(existing => append ? mergeById(existing, result.edges) : result.edges)
      setNextCursors(result.next_cursors || {})
      setError('')
    } catch (reason: any) { if (current === requestId.current) setError(reason?.response?.data?.detail || reason?.message || '任务图读取失败') }
    finally { if (current === requestId.current) { setLoading(false); setLoadingMore(false) } }
  }, [debouncedKeyword, enabled, history, inspector, nextCursors, taskType, view])

  useEffect(() => { void loadGraph(false) }, [enabled, view, inspector, history, taskType, debouncedKeyword])
  useEffect(() => { if (area) { area.contextKey = contextKey; void area.setGraph(nodes, edges) } }, [area, contextKey, edges, nodes])

  const activate = async () => {
    setActivating(true)
    try { const result = await runTaskGraphBackfill(); await updateTaskGraphConfig(true); setEnabled(true); message.success(`任务图已启用，建立 ${result.changed} 条依赖链`) }
    catch (reason: any) { message.error(reason?.response?.data?.detail || reason?.message || '任务图启用失败') }
    finally { setActivating(false) }
  }
  const showPreview = async () => { try { setPreview(await previewTaskGraphBackfill()) } catch (reason: any) { message.error(reason?.response?.data?.detail || reason?.message || '只读预览失败') } }
  const canLoadMore = Object.keys(nextCursors).length > 0
  const summary = useMemo(() => ({ editable: nodes.filter(node => node.access_mode === 'editable').length, blocked: nodes.filter(node => node.status === 'blocked').length, readonly: nodes.filter(node => node.access_mode === 'readonly').length }), [nodes])

  return (
    <div className="rete-task-flow-lab mx-auto max-w-[1680px] space-y-4 pb-4">
      <section className="app-card rete-task-flow-lab__hero"><div><div className="task-flow-lab__eyebrow"><ExperimentOutlined /> 超级管理员任务图内测</div><h1>个人任务依赖图</h1><p>每个节点都是一条真实任务；实线表示系统生成的“前置任务 → 后置任务”关系。</p></div><div className="rete-task-flow-lab__hero-actions"><Button icon={<ReloadOutlined />} loading={loading} onClick={() => void loadGraph(false)}>刷新</Button><Button type="primary" icon={<AimOutlined />} disabled={!area || !nodes.length} onClick={() => void area?.arrangeGraph()}>自动整理</Button></div></section>
      {enabled === false && <Alert type="warning" showIcon message="任务图功能尚未启用" description={<div className="flex flex-wrap gap-2 pt-2"><Button onClick={() => void showPreview()}>只读预览</Button><Button type="primary" loading={activating} onClick={() => Modal.confirm({ title: '回填并启用个人任务图？', content: '只会写入 WorkflowData 任务图表，不修改核查结果或腾讯表格。', onOk: activate })}>回填并启用</Button></div>} />}
      {error && <Alert type="error" showIcon message={error} />}
      <section className="app-card rete-task-flow-lab__toolbar"><div className="task-graph-toolbar__filters"><Segmented value={view} options={[{ label: '网格员视角', value: 'person' }, { label: '基础管控视角', value: 'queue' }]} onChange={value => setView(value as 'person' | 'queue')} />{view === 'person' && <Select showSearch value={inspector || undefined} placeholder="选择核查人" optionFilterProp="label" className="min-w-[220px]" options={options.map(item => ({ value: item.value, label: `${item.label} · ${item.count}` }))} onChange={setInspector} />}<Segmented value={history ? 'history' : 'active'} options={[{ label: '当前任务', value: 'active' }, { label: '已完成/历史', value: 'history', icon: <HistoryOutlined /> }]} onChange={value => setHistory(value === 'history')} /><Select value={taskType} className="min-w-[170px]" options={[{ value: 'all', label: '全部任务类型' }, { value: 'online_check', label: '网格员核查' }, { value: 'analysis', label: '基础管控研判' }]} onChange={setTaskType} /><Input allowClear value={keyword} placeholder="搜索当前任务图" onChange={event => setKeyword(event.target.value)} /></div><div className="rete-task-flow-lab__legend"><span>可处理 {summary.editable}</span><span>等待前置 {summary.blocked}</span><span>只读协作 {summary.readonly}</span></div></section>
      {mobile ? <section className="task-graph-mobile-list">{loading && !nodes.length ? <Skeleton active paragraph={{ rows: 6 }} /> : nodes.length ? nodes.map(node => <button key={node.id} type="button" className={`app-card task-graph-mobile-card is-${node.access_mode}`} onClick={() => navigate(node.open_path)}><span>{node.category}</span><strong>{node.title}</strong><span>{STATUS_META[node.status]?.label || node.status} · {node.owner}</span></button>) : <Empty description="当前视角没有任务" />}</section> : <section className="app-card rete-task-flow-lab__canvas-shell">{loading && !nodes.length && <div className="rete-task-flow-lab__loading"><Skeleton active paragraph={{ rows: 6 }} /></div>}{!loading && !nodes.length && enabled && <div className="rete-task-flow-lab__empty"><Empty description="当前视角没有任务" /></div>}<div ref={areaRef} className="rete-task-flow-lab__canvas" /></section>}
      {canLoadMore && <div className="flex justify-center"><Button loading={loadingMore} onClick={() => void loadGraph(true)}>继续加载各类独立任务</Button></div>}
      <Modal open={Boolean(preview)} title="个人任务图只读预览" footer={<Button type="primary" onClick={() => setPreview(null)}>知道了</Button>} onCancel={() => setPreview(null)}>{preview && <div className="task-graph-preview-grid"><div><span>当前投影</span><b>{preview.projection_rows}</b></div><div><span>无法核实</span><b>{preview.unable_to_verify}</b></div><div><span>无法核实且已研判</span><b>{preview.analyzed}</b></div><div><span>历史研判链</span><b>{preview.historical_analysis}</b></div><div><span>预计依赖链</span><b>{preview.eligible_chains}</b></div><div><span>核查人为空</span><b>{preview.blank_inspector}</b></div><div><span>账号映射异常</span><b>{preview.unmatched_inspector}</b></div></div>}</Modal>
    </div>
  )
}
