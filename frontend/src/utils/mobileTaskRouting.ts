export const FLOW_TASK_POSITIONS = new Set(['组员', '组长'])
export const POLICE_DISPATCH_TASK_POSITIONS = new Set(['基础管控', '中队长'])

export const MOBILE_TASK_TYPES = [
  '全链条',
  '出租房屋核查',
  '寄递业',
  '疑似未注销模型三',
  '疑似返苏',
] as const

export function isFlowTaskPosition(position?: string | null): boolean {
  return FLOW_TASK_POSITIONS.has(String(position || '').trim())
}

export function shouldUseMobileTaskWorkbench(
  position: string | null | undefined,
  isMobile: boolean,
): boolean {
  return isMobile && isFlowTaskPosition(position)
}

export function isPoliceDispatchTaskPosition(position?: string | null): boolean {
  return POLICE_DISPATCH_TASK_POSITIONS.has(String(position || '').trim())
}

export function shouldUsePoliceDispatchWorkbench(
  position: string | null | undefined,
  isMobile: boolean,
): boolean {
  return isMobile && isPoliceDispatchTaskPosition(position)
}
