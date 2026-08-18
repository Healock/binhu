import { useCallback, useEffect, useState, type CSSProperties } from 'react'
import { createRoot } from 'react-dom/client'
import { Alert, Button, Empty, Segmented, Skeleton, Tag } from 'antd'
import { AimOutlined, ExperimentOutlined, ReloadOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { ClassicPreset, GetSchemes, NodeEditor } from 'rete'
import { AreaPlugin, AreaExtensions } from 'rete-area-plugin'
import { ConnectionPlugin, Presets as ConnectionPresets } from 'rete-connection-plugin'
import { AutoArrangePlugin, Presets as ArrangePresets } from 'rete-auto-arrange-plugin'
import { ReactPlugin, Presets as ReactPresets, useRete, type ReactArea2D, type RenderEmit } from 'rete-react-plugin'
import {
  getPoliceDispatchWorkbench,
  listMobileTasks,
  workflowApi,
} from '../api/client'
import { MOBILE_TASK_TYPES } from '../utils/mobileTaskRouting'

type FlowNodeKind = 'trigger' | 'human' | 'wait' | 'decision' | 'terminal'

interface FlowNodeMeta {
  kind: FlowNodeKind
  title: string
  description: string
  owner: string
  count: number
  status: string
  path: string
  color: string
}

class WorkflowNode extends ClassicPreset.Node {
  meta: FlowNodeMeta
  width = 282
  height = 196
  open: () => void

  constructor(meta: FlowNodeMeta, socket: ClassicPreset.Socket, open: () => void) {
    super(meta.title)
    this.meta = meta
    this.open = open
    if (meta.kind !== 'trigger') this.addInput('in', new ClassicPreset.Input(socket, '进入', true))
    if (meta.kind !== 'terminal') this.addOutput('next', new ClassicPreset.Output(socket, '继续'))
  }
}

class WorkflowConnection extends ClassicPreset.Connection<WorkflowNode, WorkflowNode> {
  isLoop?: boolean
}

type FlowSchemes = GetSchemes<WorkflowNode, WorkflowConnection>
type FlowAreaExtra = ReactArea2D<FlowSchemes>
type FlowArea = AreaPlugin<FlowSchemes, FlowAreaExtra>
type FlowHandle = FlowArea & {
  editorInstance: NodeEditor<FlowSchemes>
  arrange: () => Promise<void>
}

const FLOW_NODES: FlowNodeMeta[] = [
  { kind: 'trigger', title: '新下发数据', description: '数据进入基础管控队列', owner: '基础管控', count: 0, status: '待进入', path: '/police-tasks', color: '#2563eb' },
  { kind: 'human', title: '审核与处理', description: '核对社区、登记情况、地址和去向', owner: '基础管控', count: 0, status: '待处理', path: '/police-tasks?status=pending_review&category=all', color: '#d97706' },
  { kind: 'human', title: '发布与分配', description: '发布任务并分配给组长或网格员', owner: '组长 / 基础管控', count: 0, status: '待处理', path: '/police-tasks?status=pending_publish&category=all', color: '#7c3aed' },
  { kind: 'human', title: '网格员核查', description: '网格员处理当前核查任务', owner: '网格员', count: 0, status: '进行中', path: '/tasks', color: '#0891b2' },
  { kind: 'wait', title: '基础管控研判', description: '无法核实任务等待研判结果', owner: '基础管控', count: 0, status: '协作节点', path: '/police-analysis', color: '#8b5cf6' },
  { kind: 'wait', title: '调取照片', description: '有明确照片工单时进入调取队列', owner: '基础管控', count: 0, status: '协作节点', path: '/photo-tasks', color: '#db2777' },
  { kind: 'terminal', title: '完成与归档', description: '结果回流并进入统计、复盘', owner: '平台', count: 0, status: '终点', path: '/summary', color: '#16a34a' },
]

function FlowNodeCard({ data, emit }: { data: FlowSchemes['Node']; emit: RenderEmit<FlowSchemes> }) {
  const flowNode = data as WorkflowNode
  const { meta } = flowNode
  return (
    <div className={`rete-task-node rete-task-node--${meta.kind}`} style={{ '--rete-node-accent': meta.color } as CSSProperties}>
      {Object.entries(flowNode.inputs).map(([key, input]) => input && <div key={key} className="rete-task-node__socket rete-task-node__socket--input"><ReactPresets.classic.RefSocket<FlowSchemes> name="input-socket" emit={emit} nodeId={flowNode.id} side="input" socketKey={key} payload={input.socket} /></div>)}
      {Object.entries(flowNode.outputs).map(([key, output]) => output && <div key={key} className="rete-task-node__socket rete-task-node__socket--output"><ReactPresets.classic.RefSocket<FlowSchemes> name="output-socket" emit={emit} nodeId={flowNode.id} side="output" socketKey={key} payload={output.socket} /></div>)}
      <div className="rete-task-node__topline"><span className="rete-task-node__kind">{meta.kind === 'wait' ? '等待协作' : meta.kind === 'terminal' ? '完成节点' : '工作节点'}</span><Tag color={meta.kind === 'wait' ? 'purple' : meta.kind === 'terminal' ? 'green' : 'blue'}>{meta.status}</Tag></div>
      <strong>{meta.title}</strong>
      <p>{meta.description}</p>
      <div className="rete-task-node__footer"><span>{meta.owner}</span><b>{meta.count}</b></div>
      <button type="button" className="rete-task-node__open" onPointerDown={event => event.stopPropagation()} onClick={flowNode.open}>进入队列</button>
    </div>
  )
}

async function createReteEditor(container: HTMLElement, openPath: (path: string) => void) {
  const editor = new NodeEditor<FlowSchemes>()
  const area = new AreaPlugin<FlowSchemes, FlowAreaExtra>(container)
  editor.use(area)
  const render = new ReactPlugin<FlowSchemes, FlowAreaExtra>({ createRoot })
  render.addPreset(ReactPresets.classic.setup({
    customize: {
      node: () => FlowNodeCard,
    },
  }))
  area.use(render)

  const connection = new ConnectionPlugin<FlowSchemes, FlowAreaExtra>()
  connection.addPreset(ConnectionPresets.classic.setup())
  area.use(connection)

  const arrange = new AutoArrangePlugin<FlowSchemes, FlowAreaExtra>()
  arrange.addPreset(ArrangePresets.classic.setup({ spacing: 80, top: 36, bottom: 36 }))
  area.use(arrange)
  AreaExtensions.simpleNodesOrder(area)

  const socket = new ClassicPreset.Socket('workflow')
  const nodes = FLOW_NODES.map(meta => new WorkflowNode({ ...meta }, socket, () => openPath(meta.path)))
  const byTitle = new Map(nodes.map(node => [node.meta.title, node]))
  for (const node of nodes) await editor.addNode(node)
  const links: Array<[string, string]> = [
    ['新下发数据', '审核与处理'],
    ['审核与处理', '发布与分配'],
    ['发布与分配', '网格员核查'],
    ['网格员核查', '基础管控研判'],
    ['网格员核查', '调取照片'],
    ['基础管控研判', '完成与归档'],
    ['调取照片', '完成与归档'],
    ['网格员核查', '完成与归档'],
  ]
  for (const [sourceTitle, targetTitle] of links) {
    const source = byTitle.get(sourceTitle)
    const target = byTitle.get(targetTitle)
    if (source && target) await editor.addConnection(new WorkflowConnection(source, 'next', target, 'in'))
  }

  const handle = area as FlowHandle
  handle.editorInstance = editor
  handle.arrange = async () => {
    await arrange.layout({ options: { 'elk.algorithm': 'layered', 'elk.direction': 'RIGHT', 'elk.spacing.nodeNode': '70' } })
    await AreaExtensions.zoomAt(area, editor.getNodes())
  }
  window.setTimeout(() => { void handle.arrange() }, 80)
  return handle
}

function updateCounts(area: FlowHandle | null, counts: Record<string, number>) {
  if (!area) return
  area.editorInstance.getNodes().forEach(flowNode => {
    const count = counts[flowNode.meta.title]
    if (typeof count !== 'number') return
    flowNode.meta.count = count
    void area.update('node', flowNode.id)
  })
}

export default function ReteTaskFlowLab() {
  const navigate = useNavigate()
  const [areaRef, area] = useRete<FlowHandle>(useCallback((container: HTMLElement) => createReteEditor(container, path => navigate(path)), [navigate]))
  const [counts, setCounts] = useState<Record<string, number>>({})
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [mode, setMode] = useState<'workflow' | 'data'>('workflow')

  const loadCounts = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [dispatch, photos, pending, analysis] = await Promise.all([
        getPoliceDispatchWorkbench(),
        workflowApi.pendingPhotoRequests({ page: 1, page_size: 1 }),
        Promise.all(MOBILE_TASK_TYPES.map(parserType => listMobileTasks({ parser_type: parserType, scope: 'all', status: 'pending', page: 1, page_size: 1 }))),
        Promise.all(MOBILE_TASK_TYPES.map(parserType => listMobileTasks({ parser_type: parserType, scope: 'all', status: 'all', review_stage: 'waiting_analysis', page: 1, page_size: 1 }))),
      ])
      const dispatchCounts = dispatch.batches.reduce((result: Record<string, number>, batch) => {
        result.review = (result.review || 0) + batch.counts.pending_review
        result.publish = (result.publish || 0) + batch.counts.pending_publish
        return result
      }, {})
      setCounts({
        '新下发数据': dispatch.batches.reduce((total, batch) => total + batch.total_count, 0),
        '审核与处理': dispatchCounts.review || 0,
        '发布与分配': dispatchCounts.publish || 0,
        '网格员核查': pending.reduce((total, page) => total + page.total, 0),
        '基础管控研判': analysis.reduce((total, page) => total + page.total, 0),
        '调取照片': photos.total,
      })
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || reason?.message || '任务数量读取失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void loadCounts() }, [loadCounts])
  useEffect(() => { updateCounts(area, counts) }, [area, counts])

  const arrange = () => {
    if (area?.arrange) void area.arrange()
  }

  return (
    <div className="rete-task-flow-lab mx-auto max-w-[1680px] space-y-4 pb-4">
      <section className="app-card rete-task-flow-lab__hero">
        <div><div className="task-flow-lab__eyebrow"><ExperimentOutlined /> Rete.js 工作流实验</div><h1>自动编排工作流</h1><p>这里展示的是“流程节点”，不是把每条任务堆满画布。任务会进入节点队列，协作节点完成后仍保留在流程中。</p></div>
        <div className="rete-task-flow-lab__hero-actions"><Button icon={<ReloadOutlined />} loading={loading} onClick={() => void loadCounts()}>刷新数量</Button><Button type="primary" icon={<AimOutlined />} onClick={arrange}>自动整理</Button></div>
      </section>
      <section className="app-card rete-task-flow-lab__toolbar">
        <Segmented value={mode} options={[{ label: '流程编排', value: 'workflow' }, { label: '任务数据', value: 'data' }]} onChange={value => setMode(value as 'workflow' | 'data')} />
        <div className="rete-task-flow-lab__legend"><span>节点 = 一类工作</span><span>连线 = 依赖关系</span><span>数字 = 当前队列</span></div>
      </section>
      {error && <Alert type="warning" showIcon message={error} />}
      {mode === 'data' && <Alert type="info" showIcon message="数据视图下一步接入具体任务队列；当前先验证流程节点、端口和自动布局。" />}
      <section className="app-card rete-task-flow-lab__canvas-shell">
        {!area && <div className="rete-task-flow-lab__loading"><Skeleton active paragraph={{ rows: 6 }} /></div>}
        <div ref={areaRef} className="rete-task-flow-lab__canvas" />
      </section>
      <section className="rete-task-flow-lab__notes"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="实验页面仅供超级管理员内测，暂不改变真实任务状态或写回腾讯文档" /></section>
    </div>
  )
}
