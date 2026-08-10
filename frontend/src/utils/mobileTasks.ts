import type {
  MobileTaskBusinessSummary,
  MobileTaskDetailData,
  MobileTaskSource,
} from '../api/client'

export interface MobileTaskSourceDifference {
  field: string
  values: string[]
}

export function mobileTaskPhoneOptions(value: string): string[] {
  const options: string[] = []
  const add = (phone: string) => {
    if (phone && !options.includes(phone)) options.push(phone)
  }

  for (const chunk of String(value || '').split(/[，,、;；|/\r\n]+/)) {
    const compact = chunk.replace(/[^\d+]/g, '')
    if (!compact) continue
    const normalized = compact.replace(/^\+?86(?=1[3-9]\d{9})/, '')
    const mobileNumbers = normalized.match(/1[3-9]\d{9}/g) || []
    if (mobileNumbers.length && mobileNumbers.join('') === normalized) {
      mobileNumbers.forEach(add)
      continue
    }
    if (normalized.length >= 5 && normalized.length <= 20) add(normalized)
  }

  if (options.length > 0) return options
  const fallback = String(value || '').replace(/[^\d+]/g, '')
  return fallback ? [fallback] : []
}

export function mobileTaskPhoneValue(value: string): string {
  return mobileTaskPhoneOptions(value)[0] || ''
}

export function mobileTaskSourceTags(value: string): string[] {
  const tags: string[] = []
  for (const part of String(value || '').split(/[\s，,、;；|/＋+]+/u)) {
    const tag = part.trim()
    if (tag && !tags.includes(tag)) tags.push(tag)
  }
  return tags
}

export function mobileTaskCanLaunchTelephone(
  userAgent: string,
  userAgentMobile = false,
  maxTouchPoints = 0,
): boolean {
  return userAgentMobile
    || /Android|iPhone|iPad|iPod|Mobile|Windows Phone/i.test(userAgent)
    || (/Macintosh/i.test(userAgent) && maxTouchPoints > 1)
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
    ...(detail.workflow.extra_edit_fields || []),
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
