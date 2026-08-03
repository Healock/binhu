import type {
  MobileTaskBusinessSummary,
  MobileTaskDetailData,
  MobileTaskSource,
} from '../api/client'

export interface MobileTaskSourceDifference {
  field: string
  values: string[]
}

export function mobileTaskPhoneValue(value: string): string {
  return value.replace(/[^\d+]/g, '')
}

export function mobileTaskSourceState(
  parserType: string,
  resultField: string,
  values: Record<string, string>,
): 'unchecked' | 'checked' | 'completed' {
  const result = (values[resultField] || '').trim()
  if (parserType === '疑似未注销模型三') {
    return ['近期反吴', '在吴', '离吴'].includes(result)
      ? 'completed'
      : 'unchecked'
  }
  if (result) return 'completed'
  return (values.现住址 || '').trim() ? 'checked' : 'unchecked'
}

export function mobileTaskSourceNeedsReview(
  resultField: string,
  secondaryFields: string[],
  values: Record<string, string>,
): boolean {
  const result = (values[resultField] || '').trim()
  return result.includes('无法核实')
    && secondaryFields.length > 0
    && secondaryFields.every(field => !(values[field] || '').trim())
}

export function buildMobileTaskChanges(
  sourceValues: Record<string, string>,
  formValues: Record<string, string>,
  editableFields: string[],
): Record<string, string> {
  return Object.fromEntries(
    editableFields
      .filter(field => (formValues[field] || '') !== (sourceValues[field] || ''))
      .map(field => [field, formValues[field] || '']),
  )
}

export function mobileTaskSourceDifferences(
  sources: Pick<MobileTaskSource, 'values'>[],
  columns: string[],
): MobileTaskSourceDifference[] {
  if (sources.length < 2) return []

  const fields = Array.from(new Set([
    ...columns,
    ...sources.flatMap(source => Object.keys(source.values)),
  ]))

  return fields.flatMap(field => {
    const values = sources.map(source => String(source.values[field] || '').trim())
    return new Set(values).size > 1 ? [{ field, values }] : []
  })
}

export function mobileTaskEditorFields(
  detail: Pick<MobileTaskDetailData, 'workflow'>,
  editableFields: string[],
  formValues: Record<string, string>,
): string[] {
  const result = formValues[detail.workflow.result_field] || ''
  const candidates = [
    '核查人',
    '现住址',
    detail.workflow.result_field,
    ...(result.includes('无法核实') ? detail.workflow.secondary_fields : []),
  ]
  return candidates.filter((field, index) => (
    candidates.indexOf(field) === index && editableFields.includes(field)
  ))
}

export function sortMobileTaskBusinesses(
  items: MobileTaskBusinessSummary[],
): MobileTaskBusinessSummary[] {
  return [...items].sort((left, right) => (
    Number(left.pending === 0) - Number(right.pending === 0)
    || right.pending - left.pending
    || left.label.localeCompare(right.label, 'zh-CN')
  ))
}
