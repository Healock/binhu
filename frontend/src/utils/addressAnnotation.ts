export const ADDRESS_STATES: Record<string, { text: string; color: string }> = {
  suggested: { text: '自动匹配', color: 'processing' }, confirmed: { text: '已确认', color: 'success' },
  ambiguous: { text: '待确认', color: 'warning' }, conflict: { text: '地址冲突', color: 'error' },
  review_required: { text: '待确认', color: 'warning' },
  unmatched: { text: '待标注', color: 'default' }, invalid: { text: '待标注', color: 'default' },
  manual_unmatched: { text: '无匹配小区', color: 'default' },
}
export const NO_MATCH_REASONS = [
  { value: 'insufficient_address', label: '地址信息不足' },
  { value: 'outside_existing_communities', label: '地址不属于现有小区' },
  { value: 'community_registry_missing', label: '小区库缺失' },
  { value: 'outside_task_community', label: '不在当前社区' },
  { value: 'other_review_required', label: '其他待核实' },
]
export const PENDING_ADDRESS_STATES = ['ambiguous', 'conflict', 'unmatched', 'invalid', 'review_required']
export function addressAnnotationUrl(task: { parser_type: string; row_key: string }) {
  return `/address-confirmation?${new URLSearchParams({ parser_type: task.parser_type, row_key: task.row_key })}`
}
export interface AnnotationLocator { parser_type: string; row_key: string; result: string }
export function rememberAnnotation(items: AnnotationLocator[], next: AnnotationLocator): AnnotationLocator[] {
  return [next, ...items.filter(item => item.parser_type !== next.parser_type || item.row_key !== next.row_key)].slice(0, 20)
}

let recentOwner: number | null = null
let recentLocators: AnnotationLocator[] = []
export function readRecentAnnotations(userId: number): AnnotationLocator[] {
  if (recentOwner !== userId) { recentOwner = userId; recentLocators = [] }
  return [...recentLocators]
}
export function saveRecentAnnotation(userId: number, next: AnnotationLocator): AnnotationLocator[] {
  recentLocators = rememberAnnotation(readRecentAnnotations(userId), { parser_type: next.parser_type, row_key: next.row_key, result: next.result })
  return [...recentLocators]
}
export function clearRecentAnnotations(): void { recentOwner = null; recentLocators = [] }
