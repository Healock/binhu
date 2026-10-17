import type { DepartmentOption } from '../api/client.ts'

export function buildDepartmentPathMap(
  departments: DepartmentOption[],
): Map<number, string> {
  const byId = new Map(departments.map(item => [item.id, item]))
  const paths = new Map<number, string>()
  const visiting = new Set<number>()
  const resolve = (id: number): string => {
    const cached = paths.get(id)
    if (cached) return cached
    const item = byId.get(id)
    if (!item) return ''
    if (visiting.has(id)) return item.name
    visiting.add(id)
    const parentPath = item.parent_id ? resolve(item.parent_id) : ''
    visiting.delete(id)
    const path = parentPath ? `${parentPath} / ${item.name}` : item.name
    paths.set(id, path)
    return path
  }
  departments.forEach(item => resolve(item.id))
  return paths
}

export function departmentOptionLabel(
  item: DepartmentOption,
  paths: Map<number, string>,
): string {
  const suffix = item.type === 'internal' ? '（历史部门）' : ''
  return `${paths.get(item.id) || item.name}${suffix}`
}

export interface DepartmentCascaderOption {
  value: number
  label: string
  selectable: boolean
  disableCheckbox: boolean
  children?: DepartmentCascaderOption[]
}

export function buildDepartmentCascader(
  departments: DepartmentOption[],
  selectedIds: number[],
  communityPosition: boolean,
): { options: DepartmentCascaderOption[]; paths: Map<number, number[]> } {
  const byId = new Map(departments.map(item => [item.id, item]))
  const selectable = new Set(
    selectableDepartments(departments, selectedIds, communityPosition).map(item => item.id),
  )
  const paths = new Map<number, number[]>()
  const children = new Map<number | null, DepartmentOption[]>()
  // Invalid historical parent references must not hide the entire branch or loop.
  for (const item of departments) {
    let parentId = byId.has(item.parent_id ?? -1) ? item.parent_id : null
    const seen = new Set([item.id])
    let ancestor = parentId
    while (ancestor != null) {
      if (seen.has(ancestor)) {
        parentId = null
        break
      }
      seen.add(ancestor)
      ancestor = byId.get(ancestor)?.parent_id ?? null
    }
    const siblings = children.get(parentId) || []
    siblings.push(item)
    children.set(parentId, siblings)
  }
  const build = (parentId: number | null, parentPath: number[]): DepartmentCascaderOption[] => {
    return (children.get(parentId) || []).flatMap(item => {
      const path = [...parentPath, item.id]
      const descendants = build(item.id, path)
      const canSelect = selectable.has(item.id)
      if (!canSelect && descendants.length === 0) return []
      paths.set(item.id, path)
      return [{
        value: item.id,
        label: `${item.name}${item.type === 'internal' ? '（历史部门）' : ''}${item.is_active ? '' : '（已停用）'}`,
        selectable: canSelect,
        disableCheckbox: !canSelect,
        ...(descendants.length ? { children: descendants } : {}),
      }]
    })
  }
  return { options: build(null, []), paths }
}

export function selectableDepartments(
  departments: DepartmentOption[],
  selectedIds: number[],
  communityPosition: boolean,
): DepartmentOption[] {
  const selected = new Set(selectedIds)
  return departments
    .filter(item => item.is_active || selected.has(item.id))
    .filter(item => communityPosition
      ? item.type === 'community' || selected.has(item.id)
      : item.type === 'unit' || selected.has(item.id))
}
