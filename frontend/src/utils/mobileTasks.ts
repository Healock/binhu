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

/**
 * 任务卡片只展示截止日期的月日。兼容腾讯常见的点、横线、斜线和中文
 * 月日格式；无法可靠识别时保留原值，避免凭空猜测业务日期。
 */
export function formatMobileTaskDeadline(value: string): string {
  const text = String(value || '').trim()
  if (!text) return ''
  const normalized = text
    .replace(/[年月/.]/g, match => (match === '年' ? '-' : match === '月' ? '-' : '-'))
    .replace(/日/g, '')
  const parts = normalized.match(/^(?:(\d{4})-)?(\d{1,2})-(\d{1,2})(?:\D.*)?$/)
  if (!parts) return text
  const month = Number(parts[2])
  const day = Number(parts[3])
  if (!Number.isInteger(month) || month < 1 || month > 12) return text
  if (!Number.isInteger(day) || day < 1 || day > 31) return text
  return `${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`
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
    return ['近期返吴', '近期反吴', '在吴', '离吴'].includes(result)
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

/**
 * 将保存响应合并回当前详情页时，保留本次明确提交的值。
 *
 * 回写接口通常会返回完整来源行，但腾讯下拉单元格在某些响应中只
 * 返回选项 ID，或者暂时省略刚写入的文本。直接用响应覆盖整行会让
 * 用户刚选中的结果在保存结束后看起来像被清空。这里只对本次提交
 * 的非空字段做安全兜底；用户明确清空的字段仍以空值为准。
 */
export function mergeMobileTaskSaveValues(
  sourceValues: Record<string, string>,
  changes: Record<string, string>,
  responseValues: Record<string, string>,
  cellMeta: Record<string, {
    type?: string
    options?: Array<{ id: string | number; text: string }>
  }> = {},
): Record<string, string> {
  const merged = { ...sourceValues, ...responseValues }

  for (const [field, requested] of Object.entries(changes)) {
    const responseValue = responseValues[field]
    const options = cellMeta[field]?.options || []
    const optionText = options.find(option => String(option.id) === String(responseValue))?.text
    if (optionText && responseValue !== optionText) {
      merged[field] = String(optionText)
      continue
    }
    if (String(requested || '').trim() && !String(responseValue || '').trim()) {
      merged[field] = requested
    }
  }

  return merged
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
  detail: Pick<MobileTaskDetailData, 'workflow' | 'analysis_mode'>,
  editableFields: string[],
  formValues: Record<string, string>,
): string[] {
  if (detail.analysis_mode) {
    return detail.workflow.analysis_fields.filter((field, index, fields) => (
      fields.indexOf(field) === index && editableFields.includes(field)
    ))
  }
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
