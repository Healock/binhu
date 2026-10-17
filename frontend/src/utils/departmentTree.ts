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
  const suffix = item.type === 'internal'
    ? '（历史部门）'
    : item.type === 'community' ? '（社区）' : '（单位）'
  return `${paths.get(item.id) || item.name}${suffix}`
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
