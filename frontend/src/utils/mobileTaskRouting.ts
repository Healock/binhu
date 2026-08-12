export const FLOW_TASK_POSITIONS = new Set(['组员', '组长'])
export const FLOW_TASK_ELEVATED_POSITIONS = new Set([
  '片长',
  '基础管控',
  '中队长',
  '社区民警',
  '所队领导',
])
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

export function isFlowTaskAdmin(
  role?: string | null,
  permissionGroupCodes: string[] = [],
): boolean {
  return ['admin', 'super_admin'].includes(String(role || '').trim())
    || permissionGroupCodes.some(code => ['admin', 'super_admin'].includes(String(code || '').trim()))
}

export function isFlowTaskElevated(
  position?: string | null,
  role?: string | null,
  permissionGroupCodes: string[] = [],
  permissions: string[] = [],
): boolean {
  return FLOW_TASK_ELEVATED_POSITIONS.has(String(position || '').trim())
    || permissions.includes('online.task.manage')
    || isFlowTaskAdmin(role, permissionGroupCodes)
}

export function canBulkAssignMobileTasks(
  position?: string | null,
  role?: string | null,
  permissionGroupCodes: string[] = [],
  permissions: string[] = [],
): boolean {
  return String(position || '').trim() === '组长'
    || isFlowTaskElevated(position, role, permissionGroupCodes, permissions)
}

export function canAccessFlowTaskWorkbench(
  position?: string | null,
  role?: string | null,
  permissionGroupCodes: string[] = [],
  permissions: string[] = [],
): boolean {
  return isFlowTaskPosition(position)
    || isFlowTaskElevated(position, role, permissionGroupCodes, permissions)
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
