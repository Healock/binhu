import type {
  QueryDataRow,
  QueryResponse,
  QuerySourceRow,
} from '../api/client.ts'

export interface QueryAuditChange {
  field: string
  before: string
  after: string
}

export type QueryDisplayRow = QueryDataRow & {
  __kind: 'parent' | 'source' | 'draft'
  __parent_key?: string
  __draft_id?: string
}

export function normalizeQueryResponse(value: QueryResponse): QueryResponse {
  return {
    ...value,
    data: Array.isArray(value.data) ? value.data : [],
    columns: Array.isArray(value.columns) ? value.columns : [],
    column_meta: Array.isArray(value.column_meta) ? value.column_meta : [],
    total: Number(value.total || 0),
    page: Number(value.page || 1),
    page_size: Number(value.page_size || 50),
    source_ready: Boolean(value.source_ready),
    writeback_enabled: Boolean(value.writeback_enabled),
    can_add: Boolean(value.can_add),
    required_fields: Array.isArray(value.required_fields)
      ? value.required_fields.map(String)
      : [],
    pending_count: Number(value.pending_count || 0),
  }
}

export function createQueryDraftRow(
  columns: string[],
  draftId: string,
): QueryDisplayRow {
  return {
    ...Object.fromEntries(columns.map(column => [column, ''])),
    __kind: 'draft',
    __draft_id: draftId,
  }
}

export function isQueryDraftTouched(
  row: QueryDisplayRow,
  columns: string[],
): boolean {
  return row.__kind === 'draft' && columns.some(
    column => String(row[column] ?? '').trim() !== '',
  )
}

export function missingQueryDraftFields(
  row: QueryDisplayRow,
  requiredFields: string[],
): string[] {
  return requiredFields.filter(
    field => String(row[field] ?? '').trim() === '',
  )
}

export function ensureTrailingQueryDraft(
  rows: QueryDisplayRow[],
  columns: string[],
  createId: () => string,
): QueryDisplayRow[] {
  const touched = rows.filter(row => isQueryDraftTouched(row, columns))
  const existingBlank = rows.find(row => !isQueryDraftTouched(row, columns))
  return [
    ...touched,
    existingBlank || createQueryDraftRow(columns, createId()),
  ]
}

export function updateQueryDraftValue(
  rows: QueryDisplayRow[],
  editedRow: QueryDisplayRow,
  column: string,
  value: unknown,
  columns: string[],
  createId: () => string,
): QueryDisplayRow[] {
  const draftId = String(editedRow.__draft_id || '')
  if (!draftId) return rows

  const latestValues = Object.fromEntries(
    columns.map(field => [field, String(editedRow[field] ?? '')]),
  )
  let matched = false
  const updated = rows.map(row => {
    if (String(row.__draft_id || '') !== draftId) return row
    matched = true
    return {
      ...row,
      ...latestValues,
      [column]: String(value ?? ''),
    }
  })

  if (!matched) {
    updated.push({
      ...createQueryDraftRow(columns, draftId),
      ...latestValues,
      [column]: String(value ?? ''),
    })
  }

  return ensureTrailingQueryDraft(updated, columns, createId)
}

export function sourceToDisplay(
  source: QuerySourceRow,
  parentKey: string,
): QueryDisplayRow {
  return {
    ...source.values,
    __kind: 'source',
    __parent_key: parentKey,
    __row_key: parentKey,
    __source_count: 1,
    __source_id: source.id,
    __revision: source.revision,
    __physical_row: source.physical_row,
    __editable_fields: source.editable_fields,
    __can_delete: source.can_delete,
  }
}

export function buildQueryDisplayRows(
  rows: QueryDataRow[],
  expanded: Record<string, QuerySourceRow[]>,
): QueryDisplayRow[] {
  const result: QueryDisplayRow[] = []
  for (const row of rows) {
    result.push({ ...row, __kind: 'parent' })
    const key = String(row.__row_key || '')
    for (const child of expanded[key] || []) {
      result.push(sourceToDisplay(child, key))
    }
  }
  return result
}

export function canEditQueryCell(
  source: 'online' | 'archive',
  row: QueryDisplayRow | undefined,
  column: string,
  canAdd = false,
): boolean {
  if (row?.__kind === 'draft') {
    return source === 'online' && canAdd
  }
  return Boolean(
    source === 'online'
    && row?.__source_id
    && row.__editable_fields?.includes(column),
  )
}

export async function saveChangedSourceFields(
  source: QuerySourceRow,
  draft: Record<string, string>,
  save: (
    column: string,
    value: string,
    expectedRevision: number,
  ) => Promise<{ revision: number }>,
): Promise<number> {
  const changed = source.editable_fields.filter(
    column => draft[column] !== source.values[column],
  )
  let revision = source.revision
  for (const column of changed) {
    const result = await save(column, draft[column] || '', revision)
    revision = result.revision
  }
  return revision
}

export function buildQueryAuditChanges(
  beforeValues: Record<string, string> | null | undefined,
  afterValues: Record<string, string> | null | undefined,
  action: 'create' | 'update' | 'delete',
): QueryAuditChange[] {
  const before = beforeValues || {}
  const after = afterValues || {}
  const fields = [...new Set([...Object.keys(before), ...Object.keys(after)])]
  return fields.flatMap(field => {
    const oldValue = String(before[field] ?? '')
    const newValue = String(after[field] ?? '')
    if (action === 'create' && !newValue.trim()) return []
    if (action === 'delete' && !oldValue.trim()) return []
    if (action === 'update' && oldValue === newValue) return []
    return [{ field, before: oldValue, after: newValue }]
  })
}
