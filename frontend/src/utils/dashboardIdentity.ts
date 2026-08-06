export function formatDashboardIdentityContext(
  departments: string[],
  scopeLabel: string,
  scopeCommunities: string[] | null,
) {
  const communityNames = new Set(scopeCommunities ?? [])
  const distinctDepartments = departments.filter(
    (name) => name && !communityNames.has(name),
  )
  return [...distinctDepartments, scopeLabel].filter(Boolean).join(' · ') || '未关联部门'
}
