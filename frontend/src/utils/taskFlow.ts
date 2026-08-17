import type { MobileTaskFilterOption, MobileTaskItem } from '../api/client'

export type TaskFlowLane = 'ready' | 'waiting' | 'exception'

export const TASK_FLOW_LANES: Array<{
  key: TaskFlowLane
  label: string
  description: string
}> = [
  { key: 'ready', label: '现在可做', description: '当前可以直接进入详情处理' },
  { key: 'waiting', label: '等待协作', description: '等待研判或其他岗位继续处理' },
  { key: 'exception', label: '需要关注', description: '来源异常或腾讯同步尚未完成' },
]

export function taskFlowLane(task: MobileTaskItem): TaskFlowLane {
  if (task.conflict || task.source_count > 1 || task.pending_sync) return 'exception'
  if (task.priority === 'waiting_analysis' || task.review_stage === 'waiting_analysis') return 'waiting'
  return 'ready'
}

export function taskFlowNodeId(task: Pick<MobileTaskItem, 'parser_type' | 'row_key'>): string {
  return `${task.parser_type}:${task.row_key}`
}

export function mergeTaskFlowInspectors(
  groups: MobileTaskFilterOption[][],
): MobileTaskFilterOption[] {
  const counts = new Map<string, number>()
  groups.flat().forEach(option => {
    const value = String(option.value || '').trim()
    if (!value || value === '__empty__') return
    counts.set(value, (counts.get(value) || 0) + Number(option.count || 0))
  })
  return [...counts.entries()]
    .map(([value, count]) => ({ value, label: value, count }))
    .sort((left, right) => right.count - left.count || left.label.localeCompare(right.label, 'zh-CN'))
}

export function defaultTaskFlowPosition(
  lane: TaskFlowLane,
  laneIndex: number,
): { x: number; y: number } {
  const x = lane === 'ready' ? 40 : lane === 'waiting' ? 400 : 760
  return { x, y: 80 + laneIndex * 196 }
}
