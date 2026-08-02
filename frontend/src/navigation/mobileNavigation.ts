import type {
  MobileDockConfig,
  MobileNavigationGroupId,
  MobileNavigationItemId,
  PermissionCode,
  Role,
} from '../types'

export type NavigationIconName =
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
    label: '数据工作台',
    dockLabel: '工作台',
    icon: 'workspace',
    items: [
      {
        id: 'online_summary',
        path: '/',
        label: '在线数据汇总',
        shortLabel: '在线汇总',
        icon: 'summary',
        end: true,
        permission: 'online.summary.view',
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
        id: 'visit_summary',
        path: '/visit-summary',
        label: '走访汇总',
        shortLabel: '走访汇总',
        icon: 'visit',
        permission: 'visit.summary.view',
      },
      {
        id: 'data_upload',
        path: '/data-upload',
        label: '数据上传中心',
        shortLabel: '数据上传',
        icon: 'upload',
        permission: 'visit.import',
        roles: ['super_admin', 'admin'],
      },
      {
        id: 'work_log',
        path: '/work-log',
        label: '工作日志生成',
        shortLabel: '工作日志',
        icon: 'worklog',
        permission: 'worklog.manage',
        roles: ['super_admin', 'admin'],
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
): boolean {
  if (item.permission && permissions) return permissions.includes(item.permission)
  return !item.roles || item.roles.includes(role)
}

export function accessibleNavigationGroups(
  role: Role,
  permissions?: PermissionCode[],
): NavigationGroupDefinition[] {
  return NAVIGATION_GROUPS.map(group => ({
    ...group,
    items: group.items.filter(item => (
      isNavigationItemAccessible(item, role, permissions)
    )),
  })).filter(group => group.items.length > 0)
}

export function defaultMobileDockConfig(
  role: Role,
  permissions?: PermissionCode[],
): MobileDockConfig {
  return {
    groups: accessibleNavigationGroups(role, permissions).map(group => ({
      id: group.id,
      items: group.items.map(item => item.id),
    })),
  }
}

export function normalizeMobileDockConfig(
  value: MobileDockConfig | null | undefined,
  role: Role,
  permissions?: PermissionCode[],
): MobileDockConfig {
  if (!value || !Array.isArray(value.groups)) {
    return defaultMobileDockConfig(role, permissions)
  }

  const definitions = new Map(
    accessibleNavigationGroups(role, permissions).map(group => [group.id, group]),
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

  return groups.length > 0
    ? { groups }
    : defaultMobileDockConfig(role, permissions)
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
  if (item.id === 'online_query' && pathname.startsWith('/tasks')) return true
  if (item.end) return pathname === item.path
  return pathname === item.path || pathname.startsWith(`${item.path}/`)
}

export function mobileNavigationItemLabel(
  item: NavigationItemDefinition,
  position?: string | null,
  short = false,
): string {
  const flowPost = position === '组员' || position === '组长'
  if (flowPost && item.id === 'online_summary') return '首页'
  if (flowPost && item.id === 'online_query') return '任务处理'
  return short ? item.shortLabel : item.label
}
