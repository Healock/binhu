export function registryVisitText(row: {
  latest_visit_date?: string | null
  visit_association_status?: string
}) {
  if (row.latest_visit_date) return row.latest_visit_date
  if (row.visit_association_status === 'community_unresolved') return '社区关联待核对'
  if (row.visit_association_status === 'ambiguous') return '档案重复，走访待核对'
  if (row.visit_association_status === 'historical_registration') return '历史户号，请查看现行档案'
  return '暂无走访记录'
}
