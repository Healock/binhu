import type {
  MobileDockConfig,
  MobileNavigationGroupId,
  MobileNavigationItemId,
  PermissionCode,
  Role,
} from '../types'

export type NavigationIconName =
  | 'dashboard'
  | 'workspace'
  | 'resources'
  | 'system'
  | 'summary'
  | 'query'
  | 'visit'
  | 'upload'
  | 'worklog'
  | 'members'
  | 'communities'
  | 'users'
  | 'settings'
  | 'operations'

export interface NavigationItemDefinition {
  id: MobileNavigationItemId
  path: string
  label: string
  shortLabel: string
  icon: NavigationIconName
  end?: boolean
  roles?: Role[]
  permission?: PermissionCode
  anyPermissions?: PermissionCode[]
}

export interface NavigationGroupDefinition {
  id: MobileNavigationGroupId
  label: string
  dockLabel: string
  icon: NavigationIconName
  items: NavigationItemDefinition[]
}

export const NAVIGATION_GROUPS: NavigationGroupDefinition[] = [
  {
    id: 'workspace',
    label: '工作台',
    dockLabel: '工作台',
    icon: 'workspace',
    items: [
      {
        id: 'dashboard',
        path: '/',
        label: '仪表盘',
        shortLabel: '首页',
        icon: 'dashboard',
        end: true,
      },
      {
        id: 'online_query',
        path: '/query',
        label: '在线数据查询',
        shortLabel: '在线查询',
        icon: 'query',
        permission: 'online.raw.view',
      },
      {
        id: 'data_upload',
        path: '/data-upload',
        label: '数据上传中心',
        shortLabel: '数据上传',
        icon: 'upload',
        anyPermissions: ['visit.import', 'police.dispatch.manage'],
        roles: ['super_admin', 'admin'],
      },
      {
        id: 'work_log',
        path: '/work-log',
        label: '文件生成',
        shortLabel: '文件生成',
        icon: 'worklog',
        permission: 'worklog.manage',
        roles: ['super_admin', 'admin'],
      },
      {
        id: 'workflow_tickets',
        path: '/workflow',
        label: '工单中心',
        shortLabel: '工单',
        icon: 'worklog',
        permission: 'workflow.ticket.view',
      },
    ],
  },
  {
    id: 'tasks',
    label: '任务处理',
    dockLabel: '任务',
    icon: 'query',
    items: [
      {
        id: 'flow_tasks',
        path: '/tasks/home',
        label: '流口指令核查',
        shortLabel: '流口核查',
        icon: 'query',
        permission: 'online.raw.view',
      },
      {
        id: 'police_tasks',
        path: '/police-tasks',
        label: '下发任务处理',
        shortLabel: '下发任务',
        icon: 'worklog',
        permission: 'police.dispatch.manage',
      },
      {
        id: 'police_analysis',
        path: '/police-analysis',
        label: '研判',
        shortLabel: '研判',
        icon: 'worklog',
        anyPermissions: ['online.task.manage', 'police.dispatch.manage'],
      },
      {
        id: 'photo_tasks',
        path: '/photo-tasks',
        label: '调照片',
        shortLabel: '调照片',
        icon: 'worklog',
        anyPermissions: ['workflow.ticket.handle', 'workflow.ticket.manage'],
      },
    ],
  },
  {
    id: 'summaries',
    label: '统计汇总',
    dockLabel: '汇总',
    icon: 'summary',
    items: [
      {
        id: 'online_summary',
        path: '/summary',
        label: '在线数据汇总',
        shortLabel: '在线汇总',
        icon: 'summary',
        end: true,
        permission: 'online.summary.view',
      },
      {
        id: 'visit_summary',
        path: '/visit-summary',
        label: '走访汇总',
        shortLabel: '走访汇总',
        icon: 'visit',
        permission: 'visit.summary.view',
      },
    ],
  },
  {
    id: 'resources',
    label: '基础资料',
    dockLabel: '基础资料',
    icon: 'resources',
    items: [
      {
        id: 'grid_members',
        path: '/grid-members',
        label: '人员管理',
        shortLabel: '人员管理',
        icon: 'members',
        permission: 'personnel.basic.view',
      },
      {
        id: 'communities',
        path: '/communities',
        label: '社区管理',
        shortLabel: '社区管理',
        icon: 'communities',
        permission: 'community.view',
      },
      {
        id: 'police_addresses',
        path: '/police-addresses',
        label: '小区管理',
        shortLabel: '小区管理',
        icon: 'communities',
        permission: 'police.address.manage',
      },
      {
        id: 'registry',
        path: '/registry',
        label: '辖区档案',
        shortLabel: '辖区档案',
        icon: 'resources',
        permission: 'registry.property.view',
      },
      {
        id: 'watch_people',
        path: '/watch-people',
        label: '人员标记',
        shortLabel: '人员标记',
        icon: 'members',
        permission: 'registry.watch.view',
      },
      {
        id: 'users',
        path: '/users',
        label: '用户管理',
        shortLabel: '用户管理',
        icon: 'users',
        permission: 'user.manage',
        roles: ['super_admin'],
      },
      {
        id: 'permission_groups',
        path: '/permission-groups',
        label: '权限组管理',
        shortLabel: '权限组',
        icon: 'users',
        permission: 'permission.manage',
        roles: ['super_admin'],
      },
    ],
  },
  {
    id: 'system',
    label: '设置',
    dockLabel: '设置',
    icon: 'system',
    items: [
      {
        id: 'settings',
        path: '/settings',
        label: '设置',
        shortLabel: '系统设置',
        icon: 'settings',
      },
      {
        id: 'workflow_config',
        path: '/settings/workflow',
        label: '工单流程配置',
        shortLabel: '流程配置',
        icon: 'worklog',
        permission: 'workflow.config.manage',
        roles: ['super_admin'],
      },
      {
        id: 'operations',
        path: '/operations',
        label: '运维中心',
        shortLabel: '运维中心',
        icon: 'operations',
        permission: 'ops.manage',
        roles: ['super_admin'],
      },
    ],
  },
]

export const MAX_DOCK_GROUPS = 4

export function isNavigationItemAccessible(
  item: NavigationItemDefinition,
  role: Role,
  permissions?: PermissionCode[],
  permissionGroupCodes: string[] = [],
  position?: string | null,
): boolean {
  const adminAccess = ['admin', 'super_admin'].includes(role)
    || permissionGroupCodes.some(code => ['admin', 'super_admin'].includes(code))
  const queryAdminAccess = permissionGroupCodes.length > 0
    ? permissionGroupCodes.some(code => ['admin', 'super_admin'].includes(code))
    : ['admin', 'super_admin'].includes(role)
  if (item.id === 'online_query' && !queryAdminAccess) return false
  if (
    item.id === 'flow_tasks'
    && !['组长', '组员', '片长', '基础管控', '中队长', '社区民警', '所队领导'].includes(position || '')
    && !permissions?.includes('online.task.manage')
    && !adminAccess
  ) return false
  if (
    item.id === 'police_tasks'
    && !['基础管控', '中队长'].includes(position || '')
    && !(
      !position
      && (
        adminAccess
      )
    )
  ) return false
  if (
    item.id === 'police_analysis'
    && !permissions?.some(permission => (
      permission === 'online.task.manage' || permission === 'police.dispatch.manage'
    ))
    && !adminAccess
  ) return false
  if (
    item.id === 'photo_tasks'
    && position !== '基础管控'
    && !permissions?.includes('workflow.ticket.manage')
    && !adminAccess
  ) return false
  if (item.permission) {
    // Permission data is authoritative.  If it is unavailable, fail closed
    // for permission-gated items instead of exposing a menu by accident.
    return permissions
      ? permissions.includes(item.permission)
      : role === 'super_admin' || Boolean(item.roles?.includes(role))
  }
  if (item.anyPermissions) {
    return permissions
      ? item.anyPermissions.some(permission => permissions.includes(permission))
      : role === 'super_admin' || Boolean(item.roles?.includes(role))
  }
  return !item.roles || item.roles.includes(role)
}

export function accessibleNavigationGroups(
  role: Role,
  permissions?: PermissionCode[],
  permissionGroupCodes: string[] = [],
  position?: string | null,
): NavigationGroupDefinition[] {
  return NAVIGATION_GROUPS.map(group => ({
    ...group,
    items: group.items.filter(item => (
      isNavigationItemAccessible(item, role, permissions, permissionGroupCodes, position)
    )),
  })).filter(group => group.items.length > 0)
}

export function defaultMobileDockConfig(
  role: Role,
  permissions?: PermissionCode[],
  permissionGroupCodes: string[] = [],
  position?: string | null,
): MobileDockConfig {
  return {
    version: 2,
    groups: accessibleNavigationGroups(role, permissions, permissionGroupCodes, position).slice(0, MAX_DOCK_GROUPS).map(group => ({
      id: group.id,
      items: group.items.map(item => item.id),
    })),
  }
}

export function normalizeMobileDockConfig(
  value: MobileDockConfig | null | undefined,
  role: Role,
  permissions?: PermissionCode[],
  permissionGroupCodes: string[] = [],
  position?: string | null,
): MobileDockConfig {
  if (!value || value.version !== 2 || !Array.isArray(value.groups)) {
    return defaultMobileDockConfig(role, permissions, permissionGroupCodes, position)
  }

  const definitions = new Map(
    accessibleNavigationGroups(role, permissions, permissionGroupCodes, position).map(group => [group.id, group]),
  )
  const rawTaskItems = new Set(
    value.groups
      .filter(group => group.id === 'tasks' && Array.isArray(group.items))
      .flatMap(group => group.items),
  )
  const seenGroups = new Set<MobileNavigationGroupId>()
  const groups = value.groups.slice(0, MAX_DOCK_GROUPS).flatMap((rawGroup) => {
    const definition = definitions.get(rawGroup.id)
    if (!definition || seenGroups.has(rawGroup.id)) return []
    const allowedItems = new Set(definition.items.map(item => item.id))
    const seenItems = new Set<MobileNavigationItemId>()
    const items = (Array.isArray(rawGroup.items) ? rawGroup.items : [])
      .flatMap((itemId) => {
        if (!allowedItems.has(itemId) || seenItems.has(itemId)) return []
        seenItems.add(itemId)
        return [itemId]
      })
    if (items.length === 0) return []
    seenGroups.add(rawGroup.id)
    return [{ id: rawGroup.id, items }]
  })

  const normalized = groups.length > 0
    ? groups
    : defaultMobileDockConfig(role, permissions, permissionGroupCodes, position).groups
  const workspaceDefinition = definitions.get('workspace')
  let workspace = normalized.find(group => group.id === 'workspace')
  if (workspaceDefinition) {
    const dashboardId: MobileNavigationItemId = 'dashboard'
    if (workspace) {
      workspace.items = [dashboardId, ...workspace.items.filter(item => item !== dashboardId)]
    } else {
      workspace = { id: 'workspace', items: [dashboardId] }
      normalized.unshift(workspace)
    }
    if (
      rawTaskItems.has('workflow_tickets')
      && workspaceDefinition.items.some(item => item.id === 'workflow_tickets')
      && !workspace.items.includes('workflow_tickets')
    ) workspace.items.push('workflow_tickets')
  }
  const tasks = normalized.find(group => group.id === 'tasks')
  const taskDefinition = definitions.get('tasks')
  if (tasks && taskDefinition) {
    const appendMovedTask = (
      previousId: MobileNavigationItemId,
      nextId: MobileNavigationItemId,
    ) => {
      if (
        rawTaskItems.has(previousId)
        && taskDefinition.items.some(item => item.id === nextId)
        && !tasks.items.includes(nextId)
      ) tasks.items.push(nextId)
    }
    appendMovedTask('police_tasks', 'police_analysis')
    appendMovedTask('workflow_tickets', 'photo_tasks')
  }
  const presentGroups = new Set(normalized.map(group => group.id))
  for (const definition of accessibleNavigationGroups(
    role,
    permissions,
    permissionGroupCodes,
    position,
  )) {
    if (normalized.length >= MAX_DOCK_GROUPS) break
    if (presentGroups.has(definition.id)) continue
    normalized.push({
      id: definition.id,
      items: definition.items.map(item => item.id),
    })
    presentGroups.add(definition.id)
  }
  return {
    version: 2,
    groups: [
      ...normalized.filter(group => group.id === 'workspace'),
      ...normalized.filter(group => group.id !== 'workspace'),
    ].slice(0, MAX_DOCK_GROUPS),
  }
}

function moveEntry<T>(items: T[], fromIndex: number, toIndex: number): T[] {
  if (
    fromIndex < 0
    || toIndex < 0
    || fromIndex >= items.length
    || toIndex >= items.length
    || fromIndex === toIndex
  ) return items
  const next = [...items]
  const [entry] = next.splice(fromIndex, 1)
  next.splice(toIndex, 0, entry)
  return next
}

export function reorderMobileDockGroups(
  config: MobileDockConfig,
  activeGroupId: MobileNavigationGroupId,
  overGroupId: MobileNavigationGroupId,
): MobileDockConfig {
  const fromIndex = config.groups.findIndex(group => (
    group.id === activeGroupId
  ))
  const toIndex = config.groups.findIndex(group => group.id === overGroupId)
  return {
    version: 2,
    groups: moveEntry(config.groups, fromIndex, toIndex),
  }
}

export function reorderMobileDockItems(
  config: MobileDockConfig,
  groupId: MobileNavigationGroupId,
  activeItemId: MobileNavigationItemId,
  overItemId: MobileNavigationItemId,
): MobileDockConfig {
  return {
    version: 2,
    groups: config.groups.map((group) => {
      if (group.id !== groupId) return group
      return {
        ...group,
        items: moveEntry(
          group.items,
          group.items.indexOf(activeItemId),
          group.items.indexOf(overItemId),
        ),
      }
    }),
  }
}

export function navigationGroupById(
  id: MobileNavigationGroupId,
): NavigationGroupDefinition | undefined {
  return NAVIGATION_GROUPS.find(group => group.id === id)
}

export function navigationItemById(
  id: MobileNavigationItemId,
): NavigationItemDefinition | undefined {
  return NAVIGATION_GROUPS
    .flatMap(group => group.items)
    .find(item => item.id === id)
}

export function routeIsActive(
  pathname: string,
  item: NavigationItemDefinition,
): boolean {
  if (item.id === 'flow_tasks' && pathname.startsWith('/tasks')) return true
  if (item.end) return pathname === item.path
  return pathname === item.path || pathname.startsWith(`${item.path}/`)
}

export function mobileNavigationItemLabel(
  item: NavigationItemDefinition,
  position?: string | null,
  short = false,
): string {
  if (item.id === 'dashboard') return '首页'
  void position
  return short ? item.shortLabel : item.label
}
