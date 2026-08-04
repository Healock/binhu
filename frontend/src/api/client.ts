import axios from 'axios'
import type {
  Spreadsheet, SpreadsheetCreate, StatsResponse, StatsItem,
  SyncStatus, SyncTriggerResponse, SyncSchedule, AppNotification,
  OAuthConfig, OAuthStatus, OpsOverview, OpsDatabase, BackupSchedule,
  BackupJob, AuditEvent, User, UserPreferences, ReportColumnMode,
  WorkLogDraft, WorkLogDraftSummary, WorkLogMissingItem, WorkLogSchema,
} from '../types'

const api = axios.create({
  baseURL: '/api',
  timeout: 30000,
  withCredentials: true,
})
const activeRequest = { headers: { 'X-User-Activity': '1' } }
let unauthorizedRedirectStarted = false

export function resetUnauthorizedRedirectForTests(): void {
  unauthorizedRedirectStarted = false
}

export interface AuthFetchOptions {
  handleUnauthorized?: boolean
  markActivity?: boolean
}

export function handleUnauthorized(detail?: unknown): void {
  if (window.location.pathname.includes('/login') || unauthorizedRedirectStarted) {
    return
  }
  const payload = detail && typeof detail === 'object'
    ? detail as { code?: unknown; message?: unknown }
    : null
  const code = typeof payload?.code === 'string'
    ? payload.code
    : 'session_expired'
  const message = typeof payload?.message === 'string'
    ? payload.message
    : '登录状态已失效'
  unauthorizedRedirectStarted = true
  sessionStorage.setItem('auth_exit_reason', JSON.stringify({ code, message }))
  window.location.href = '/login'
}

export async function fetchWithAuth(
  input: RequestInfo | URL,
  init: RequestInit = {},
  options: AuthFetchOptions = {},
): Promise<Response> {
  const headers = new Headers(init.headers)
  const method = (init.method || 'GET').toUpperCase()
  if (
    options.markActivity !== false
    && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)
    && !headers.has('X-User-Activity')
  ) {
    headers.set('X-User-Activity', '1')
  }
  const response = await fetch(input, {
    ...init,
    credentials: init.credentials || 'include',
    headers,
  })
  if (response.status === 401 && options.handleUnauthorized !== false) {
    const body = await response.clone().json().catch(() => null)
    const detail = body && typeof body === 'object'
      ? (body as { detail?: unknown }).detail
      : null
    handleUnauthorized(detail)
  }
  return response
}

api.interceptors.request.use((config) => {
  const method = (config.method || 'get').toLowerCase()
  if (
    ['post', 'put', 'patch', 'delete'].includes(method)
    && config.headers.get('X-User-Activity') !== '0'
  ) {
    config.headers.set('X-User-Activity', '1')
  }
  return config
})

// 401 拦截器：跳转登录页
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401) {
      handleUnauthorized(error?.response?.data?.detail)
    }
    return Promise.reject(error)
  }
)

export async function getCurrentUser(): Promise<User> {
  const { data } = await api.get('/auth/me')
  return data.user
}

export async function recordSessionActivity(): Promise<User> {
  const { data } = await api.post('/auth/activity')
  return data.user
}

export async function changeOwnPassword(
  currentPassword: string,
  newPassword: string,
): Promise<void> {
  await api.put('/auth/password', {
    current_password: currentPassword,
    new_password: newPassword,
  })
}

export interface PermissionCatalogItem {
  code: string
  category: string
  label: string
}

export interface PermissionGroupItem {
  id: number
  code: string
  name: string
  description: string
  permissions: string[]
  data_scope: 'all' | 'own_department'
  is_system: boolean
  is_locked: boolean
  user_count: number
  positions: string[]
}

export async function getPermissionCatalog(): Promise<{
  permissions: PermissionCatalogItem[]
  data_scopes: Array<{ value: string; label: string }>
}> {
  const { data } = await api.get('/permission-groups/catalog')
  return data
}

export async function getPermissionGroups(): Promise<{
  data: PermissionGroupItem[]
  position_mappings: Record<string, number[]>
  position_user_counts: Record<string, number>
}> {
  const { data } = await api.get('/permission-groups')
  return data
}

export async function createPermissionGroup(payload: {
  name: string
  description: string
  permissions: string[]
  data_scope: string
}): Promise<void> {
  await api.post('/permission-groups', payload)
}

export async function updatePermissionGroup(
  id: number,
  payload: {
    name: string
    description: string
    permissions: string[]
    data_scope: string
  },
): Promise<{ affected_users: number }> {
  const { data } = await api.put(`/permission-groups/groups/${id}`, payload)
  return data
}

export async function deletePermissionGroup(id: number): Promise<void> {
  await api.delete(`/permission-groups/groups/${id}`)
}

export async function updatePositionPermissionMappings(
  mappings: Record<string, number[]>,
): Promise<{ affected_users: number }> {
  const { data } = await api.put('/permission-groups/position-mappings/all', {
    mappings,
  })
  return data
}

// ---- Spreadsheets ----
export async function listSpreadsheets(): Promise<Spreadsheet[]> {
  const { data } = await api.get('/spreadsheets')
  return data
}

export async function createSpreadsheet(payload: SpreadsheetCreate): Promise<Spreadsheet> {
  const { data } = await api.post('/spreadsheets', payload)
  return data
}

export async function updateSpreadsheet(id: number, payload: Partial<SpreadsheetCreate>): Promise<Spreadsheet> {
  const { data } = await api.put(`/spreadsheets/${id}`, payload)
  return data
}

export async function deleteSpreadsheet(id: number): Promise<void> {
  await api.delete(`/spreadsheets/${id}`)
}

export async function getParserTypes(): Promise<string[]> {
  const { data } = await api.get('/spreadsheets/meta/parser-types')
  return data.data
}

export async function getSpreadsheetsConfig(): Promise<Record<string, string>> {
  const { data } = await api.get('/spreadsheets/config')
  const map: Record<string, string> = {}
  data.data.forEach((item: { parser_type: string; url: string }) => {
    map[item.parser_type] = item.url
  })
  return map
}

export async function saveSpreadsheetsConfig(configs: Record<string, string>): Promise<void> {
  await api.put('/spreadsheets/config', { configs })
}

// ---- Sync ----
export async function triggerSync(): Promise<SyncTriggerResponse> {
  const { data } = await api.post('/sync/trigger')
  return data
}

export async function getSyncStatus(): Promise<SyncStatus> {
  const { data } = await api.get('/sync/status')
  return data
}

export async function getSyncSchedule(): Promise<SyncSchedule> {
  const { data } = await api.get('/sync/schedule')
  return data
}

export async function updateSyncSchedule(payload: {
  enabled: boolean
  interval_minutes: number
}): Promise<SyncSchedule & { message: string }> {
  const { data } = await api.put('/sync/schedule', payload)
  return data
}

// ---- Notifications / 站内信 ----
export async function getNotifications(limit = 20): Promise<{
  unread_count: number
  personal_unread_count: number
  announcement_unread_count: number
  data: AppNotification[]
}> {
  const { data } = await api.get('/notifications', { params: { limit } })
  return data
}

export async function getNotificationUnreadCount(): Promise<{
  unread_count: number
  personal_unread_count: number
  announcement_unread_count: number
}> {
  const { data } = await api.get('/notifications/unread-count')
  return data
}

export async function markNotificationRead(
  notification: Pick<AppNotification, 'id' | 'source'>,
): Promise<void> {
  if (notification.source === 'announcement') {
    await api.post(`/notifications/announcements/${notification.id}/read`)
    return
  }
  await api.post(`/notifications/${notification.id}/read`)
}

export async function markAllNotificationsRead(): Promise<void> {
  await api.post('/notifications/read-all')
}

export async function createAnnouncement(payload: {
  title: string
  content: string
  severity: 'info' | 'warning'
}): Promise<void> {
  await api.post('/notifications/announcements', payload)
}

export async function deleteAnnouncement(id: number): Promise<void> {
  await api.delete(`/notifications/announcements/${id}`)
}

// ---- Super-admin operations center ----
export async function getOpsOverview(): Promise<OpsOverview> {
  const { data } = await api.get('/admin/ops/overview')
  return data
}

export async function getOpsDatabases(): Promise<OpsDatabase[]> {
  const { data } = await api.get('/admin/ops/databases')
  return data.data
}

export async function getOpsDatabaseTables(database: string): Promise<any[]> {
  const { data } = await api.get(`/admin/ops/databases/${encodeURIComponent(database)}/tables`)
  return data.data
}

export async function getOpsTableStructure(database: string, table: string): Promise<any> {
  const { data } = await api.get(
    `/admin/ops/databases/${encodeURIComponent(database)}/tables/${encodeURIComponent(table)}`,
  )
  return data
}

export async function getBackups(): Promise<{
  data: BackupJob[]
  legacy_files: Array<{ filename: string; size_bytes: number; modified_at: string }>
  schedule: BackupSchedule
}> {
  const { data } = await api.get('/admin/ops/backups')
  return data
}

export async function triggerDatabaseBackup(): Promise<{
  task_id: number
  status: string
  message: string
}> {
  const { data } = await api.post('/admin/ops/backups')
  return data
}

export async function updateBackupSchedule(payload: {
  enabled: boolean
  run_hour: number
  run_minute: number
}): Promise<BackupSchedule> {
  const { data } = await api.put('/admin/ops/backup-schedule', payload)
  return data
}

export async function downloadBackup(jobId: number, password: string): Promise<Blob> {
  const { data } = await api.post(
    `/admin/ops/backups/${jobId}/download`,
    { password },
    { responseType: 'blob', timeout: 120000 },
  )
  return data
}

export async function getAuditEvents(params: {
  page: number
  page_size: number
  action?: string
}): Promise<{ data: AuditEvent[]; total: number; page: number; page_size: number }> {
  const { data } = await api.get('/admin/ops/audit', { params })
  return data
}

export async function downloadDiagnostics(): Promise<Blob> {
  const { data } = await api.post(
    '/admin/ops/diagnostics',
    null,
    { responseType: 'blob', timeout: 120000 },
  )
  return data
}

// ---- Work logs / 工作日志 ----
export async function getWorkLogSchema(): Promise<WorkLogSchema> {
  const { data } = await api.get('/work-logs/schema')
  return data
}

export async function listWorkLogDrafts(params: {
  page: number
  page_size: number
  start_date?: string
  end_date?: string
  keyword?: string
}): Promise<{
  data: WorkLogDraftSummary[]
  total: number
  page: number
  page_size: number
}> {
  const { data } = await api.get('/work-logs/drafts', { params })
  return data
}

export async function getWorkLogDraft(
  reportType: 'daily',
  businessDate: string,
): Promise<WorkLogDraft> {
  const { data } = await api.get(
    `/work-logs/drafts/by-date/${reportType}/${businessDate}`,
  )
  return data
}

export async function createWorkLogDraft(
  reportType: 'daily',
  businessDate: string,
): Promise<WorkLogDraft> {
  const { data } = await api.post('/work-logs/drafts', {
    report_type: reportType,
    business_date: businessDate,
  })
  return data
}

export async function saveWorkLogDraft(
  draftId: number,
  payload: {
    version: number
    manual_values: Record<string, unknown>
    override_values: Record<string, unknown>
  },
): Promise<WorkLogDraft> {
  const { data } = await api.put(`/work-logs/drafts/${draftId}`, payload, {
    headers: { 'X-User-Activity': '0' },
  })
  return data
}

export async function deleteWorkLogDraft(draftId: number): Promise<void> {
  await api.delete(`/work-logs/drafts/${draftId}`)
}

export async function takeoverWorkLogDraft(
  draftId: number,
): Promise<WorkLogDraft> {
  const { data } = await api.post(`/work-logs/drafts/${draftId}/takeover`)
  return data
}

export async function refreshWorkLogDraft(
  draftId: number,
  version: number,
): Promise<WorkLogDraft> {
  const { data } = await api.post(`/work-logs/drafts/${draftId}/refresh`, {
    version,
  })
  return data
}

export async function getWorkLogMissing(
  draftId: number,
): Promise<{ missing: WorkLogMissingItem[]; count: number }> {
  const { data } = await api.get(`/work-logs/drafts/${draftId}/missing`)
  return data
}

export async function exportWorkLog(draftId: number): Promise<Blob> {
  const { data } = await api.post(
    `/work-logs/drafts/${draftId}/export`,
    null,
    { responseType: 'blob', timeout: 120000 },
  )
  return data
}

// ---- Stats / 日报 ----
export async function getReportTypes(): Promise<{ data: string[]; implemented: string[] }> {
  const { data } = await api.get('/stats/types')
  return data
}

export interface SummaryReportConfig {
  available_types: string[]
  selected_types: string[]
  message?: string
}

export async function getSummaryReportConfig(): Promise<SummaryReportConfig> {
  const { data } = await api.get('/stats/summary-config')
  return data
}

export async function updateSummaryReportConfig(
  types: string[],
): Promise<SummaryReportConfig> {
  const { data } = await api.put('/stats/summary-config', { types })
  return data
}

export interface OnlineDataOverview {
  exists: boolean
  parser_type: string
  start_date: string
  end_date: string
  available_start_date: string | null
  available_end_date: string | null
  available_data_days: number
  selected_data_days: number
  total_tasks: number
  carryover_tasks: number
  new_tasks: number
  changed_tasks: number
  pending_tasks: number
  completed_tasks: number
  completion_rate: number
}

export async function getOnlineDataOverview(
  startDate: string,
  endDate: string,
  parserType: string,
): Promise<OnlineDataOverview> {
  const { data } = await api.get('/stats/overview', {
    params: {
      start_date: startDate,
      end_date: endDate,
      parser_type: parserType,
    },
  })
  return data
}

export async function saveUserPreferences(payload: UserPreferences): Promise<User> {
  const { data } = await api.put('/auth/preferences', payload)
  return data.user
}

export async function getReport(
  date: string,
  parser_type?: string,
  columnMode?: ReportColumnMode,
): Promise<any> {
  const { data } = await api.get('/stats/report', {
    params: {
      report_date: date,
      parser_type: parser_type || '全链条',
      column_mode: columnMode,
    },
  })
  return data
}

export async function getReportRange(
  startDate: string,
  endDate: string,
  parserType: string,
  columnMode?: ReportColumnMode,
): Promise<any> {
  const { data } = await api.get('/stats/report_range', {
    params: {
      start_date: startDate,
      end_date: endDate,
      parser_type: parserType,
      column_mode: columnMode,
    },
  })
  return data
}

export async function getTodayReport(): Promise<{ exists: boolean; columns?: string[]; data?: Record<string, any>[] }> {
  const { data } = await api.get('/stats/today')
  return data
}

// ---- Query ----
export async function getQueryTypes(): Promise<string[]> {
  const { data } = await api.get('/query/types')
  return data.data
}

export interface QueryColumnMeta {
  field: string
  type: 'text' | 'number' | 'select'
  multiple?: boolean
  options?: Array<{ id: string | number; text: string }>
}

export interface QueryDataRow extends Record<string, unknown> {
  __row_key?: string
  __source_count?: number
  __conflict?: boolean
  __pending?: boolean
  __source_id?: number | null
  __revision?: number | null
  __physical_row?: number | null
  __editable_fields?: string[]
  __can_delete?: boolean
}

export interface QuerySourceRow {
  id: number
  physical_row: number
  values: Record<string, string>
  cell_meta: Record<string, Omit<QueryColumnMeta, 'field'>>
  revision: number
  row_hash: string
  editable_fields: string[]
  can_delete: boolean
}

export interface QueryResponse {
  data: QueryDataRow[]
  total: number
  page: number
  page_size: number
  columns: string[]
  column_meta: QueryColumnMeta[]
  source_ready: boolean
  writeback_enabled: boolean
  can_add: boolean
  required_fields: string[]
  pending_count: number
  scope_message?: string
  row_manage_message?: string
}

export async function queryData(params: {
  type: string
  source?: string
  page?: number
  page_size?: number
  keyword?: string
  sort_by?: string
  sort_order?: string
  filters?: Record<string, string[]>
  grid_filters?: Record<string, unknown>
}): Promise<QueryResponse> {
  const { data } = await api.get(`/query/${params.type}`, {
    ...activeRequest,
    params: {
      source: params.source || 'online',
      page: params.page || 1,
      page_size: params.page_size || 50,
      keyword: params.keyword,
      sort_by: params.sort_by,
      sort_order: params.sort_order,
      filters: params.filters ? JSON.stringify(params.filters) : undefined,
      grid_filters: params.grid_filters
        ? JSON.stringify(params.grid_filters)
        : undefined,
    },
  })
  return data
}

export async function getQuerySourceRows(
  type: string,
  rowKey: string,
): Promise<QuerySourceRow[]> {
  const { data } = await api.get(`/query/${type}/rows/${rowKey}/sources`)
  return data.data
}

export async function updateQuerySourceCell(
  type: string,
  sourceId: number,
  payload: { column: string; value: string; expected_revision: number },
): Promise<{
  values: Record<string, string>
  row_key: string
  revision: number
  pending_sync: boolean
  message: string
}> {
  const { data } = await api.patch(`/query/${type}/source-rows/${sourceId}`, payload)
  return data
}

export async function createQuerySourceRow(
  type: string,
  values: Record<string, string>,
): Promise<{ message: string; row_key: string; pending_sync: boolean }> {
  const { data } = await api.post(`/query/${type}/source-rows`, { values })
  return data
}

export async function deleteQuerySourceRow(
  type: string,
  sourceId: number,
  expectedRevision: number,
): Promise<{ message: string; pending_sync: boolean }> {
  const { data } = await api.delete(`/query/${type}/source-rows/${sourceId}`, {
    params: { expected_revision: expectedRevision },
  })
  return data
}

// ---- Flow-post mobile task workbench ----
export type MobileTaskScope = 'mine' | 'community'
export type MobileTaskStatus = 'pending' | 'review' | 'completed' | 'all'
export type MobileTaskState = 'unchecked' | 'checked' | 'completed'
export type MobileTaskReviewStage = 'all' | 'waiting_analysis' | 'analyzed'

export interface MobileTaskBusinessSummary {
  parser_type: string
  label: string
  pending: number
  unchecked: number
  checked: number
  completed: number
  review: number
  source_ready: boolean
}

export interface MobileTaskHomeData {
  business_date: string
  last_success_at: string | null
  scope: MobileTaskScope
  person: { name: string; position: string; community: string }
  personal: {
    pending: number
    new_today: number | null
    carryover_today: number | null
    completed_today: number | null
  }
  community: { pending: number }
  daily_snapshot_available: boolean
  businesses: MobileTaskBusinessSummary[]
}

export interface MobileTaskSummaryFields {
  title: string
  identity_number: string
  phone: string
  source: string
  address: string
  date: string
  result: string
}

export interface MobileTaskItem {
  row_key: string
  parser_type: string
  summary: MobileTaskSummaryFields
  community: string
  inspector: string
  state: MobileTaskState
  needs_review: boolean
  review_stage: '' | 'waiting_analysis' | 'analyzed'
  source_count: number
  conflict: boolean
  pending_sync: boolean
}

export interface MobileTaskSource {
  id: number
  physical_row: number
  values: Record<string, string>
  cell_meta: Record<string, Omit<QueryColumnMeta, 'field'>>
  revision: number
  row_hash: string
  editable_fields: string[]
  state: MobileTaskState
  needs_review: boolean
  review_stage: '' | 'waiting_analysis' | 'analyzed'
}

export interface MobileTaskDetailData {
  task: MobileTaskItem
  workflow: {
    result_field: string
    phone_fields: string[]
    title_fields: string[]
    address_fields: string[]
    date_fields: string[]
    secondary_fields: string[]
    analysis_fields: string[]
    columns: string[]
  }
  writeback_enabled: boolean
  sources: MobileTaskSource[]
}

export async function getMobileTaskHome(
  scope: MobileTaskScope,
): Promise<MobileTaskHomeData> {
  const { data } = await api.get('/mobile-tasks/home', {
    ...activeRequest,
    params: { scope },
  })
  return data
}

export async function listMobileTasks(params: {
  parser_type: string
  scope: MobileTaskScope
  status: MobileTaskStatus
  review_stage?: MobileTaskReviewStage
  keyword?: string
  page?: number
  page_size?: number
}): Promise<{
  data: MobileTaskItem[]
  total: number
  page: number
  page_size: number
  source_ready: boolean
  message: string
}> {
  const { data } = await api.get(`/mobile-tasks/${encodeURIComponent(params.parser_type)}`, {
    ...activeRequest,
    params: {
      scope: params.scope,
      status: params.status,
      review_stage: params.review_stage || 'all',
      keyword: params.keyword,
      page: params.page || 1,
      page_size: params.page_size || 20,
    },
  })
  return data
}

export async function getMobileTaskDetail(
  parserType: string,
  rowKey: string,
): Promise<MobileTaskDetailData> {
  const { data } = await api.get(
    `/mobile-tasks/${encodeURIComponent(parserType)}/${encodeURIComponent(rowKey)}`,
    activeRequest,
  )
  return data
}

export async function updateMobileTask(
  parserType: string,
  sourceId: number,
  payload: { changes: Record<string, string>; expected_revision: number },
): Promise<{
  values: Record<string, string>
  row_key: string
  revision: number
  pending_sync: boolean
  message: string
}> {
  const { data } = await api.patch(
    `/mobile-tasks/${encodeURIComponent(parserType)}/source-rows/${sourceId}`,
    payload,
  )
  return data
}

export interface QueryWritebackAudit {
  id: number
  username: string
  action: 'create' | 'update' | 'delete'
  parser_type: string
  spreadsheet_id: number
  physical_row: number | null
  column_name: string | null
  row_key_before: string | null
  row_key_after: string | null
  before_values: Record<string, string> | null
  after_values: Record<string, string> | null
  sync_status: 'pending' | 'synced' | 'failed'
  synced_at: string | null
  created_at: string
}

export async function getQueryWritebackAudit(params: {
  page?: number
  page_size?: number
  parser_type?: string
}): Promise<{ data: QueryWritebackAudit[]; total: number }> {
  const { data } = await api.get('/query/writeback/audit', { params })
  return data
}

// ---- Grid Members ----
export interface GridMember {
  id: number
  name: string
  community: string
  position: string
  phone?: string
  notes?: string
  status: string
  effective_status: string
  status_detail: string
  leave_start_date?: string | null
  leave_end_date?: string | null
  leave_reason?: string
  leave_source?: string
  leave_state: 'active' | 'upcoming' | 'expired' | null
  has_id_card?: boolean
  id_card_masked?: string
  id_card_number?: string
  department_id: number | null
  department_ids: number[]
  department: DepartmentOption | null
  departments: DepartmentOption[]
  community_names: string[]
  account: {
    id: number
    username_masked: string
    display_name: string
  } | null
}

export interface GridMemberPayload {
  name?: string
  community?: string
  department_id?: number | null
  department_ids?: number[]
  position?: string
  phone?: string
  id_card_number?: string | null
  account_id?: number
  notes?: string
  status?: '在岗' | '离岗'
  leave_start_date?: string | null
  leave_end_date?: string | null
  leave_reason?: string
  leave_source?: string
}

export interface GridCommunity {
  id: number
  name: string
  grid_count: number
  aliases: string[]
  police_officers: string[]
  police_officer_ids: number[]
  area_id: number | null
  area_name: string
  is_active: boolean
}

export interface CommunityArea {
  id: number
  name: string
  community_count: number
  leaders: Array<{ id: number; name: string }>
  leader_ids: number[]
}

export interface DepartmentOption {
  id: number
  name: string
  type: 'community' | 'internal'
  community_name: string | null
  is_active: boolean
}

export async function getDepartments(): Promise<DepartmentOption[]> {
  const { data } = await api.get('/grid-members/departments')
  return data.data
}

export interface AccountOption {
  id: number
  username_masked: string
  display_name: string
  linked_member_id?: number | null
  linked_member_name?: string
  is_current?: boolean
}

export async function getUnlinkedAccountOptions(): Promise<AccountOption[]> {
  const { data } = await api.get('/grid-members/unlinked-accounts')
  return data.data
}

export async function getMemberAccountOptions(memberId: number): Promise<AccountOption[]> {
  const { data } = await api.get('/grid-members/account-options', {
    params: { member_id: memberId },
  })
  return data.data
}

export async function getCommunityPoliceOptions(): Promise<Array<{ id: number; name: string }>> {
  const { data } = await api.get('/grid-members/community-police-options')
  return data.data
}

export async function getCommunityAreas(): Promise<CommunityArea[]> {
  const { data } = await api.get('/grid-members/areas')
  return data.data
}

export async function getAreaLeaderOptions(): Promise<Array<{ id: number; name: string }>> {
  const { data } = await api.get('/grid-members/area-leader-options')
  return data.data
}

export async function createCommunityArea(payload: {
  name: string
  leader_ids: number[]
}): Promise<void> {
  await api.post('/grid-members/areas', payload)
}

export async function updateCommunityArea(
  id: number,
  payload: { name: string; leader_ids: number[] },
): Promise<void> {
  await api.put(`/grid-members/areas/${id}`, payload)
}

export async function deleteCommunityArea(id: number): Promise<void> {
  await api.delete(`/grid-members/areas/${id}`)
}

export async function listGridMembers(params: {
  keyword?: string
  community?: string
  position?: string
  category?: 'flow_work' | 'internal_business' | 'police_leadership'
  page?: number
  page_size?: number
}): Promise<{
  data: GridMember[]
  total: number
  page: number
  page_size: number
  category_counts: Record<string, number>
}> {
  const { data } = await api.get('/grid-members', { params })
  return data
}

export async function getGridCommunities(): Promise<GridCommunity[]> {
  const { data } = await api.get('/grid-members/communities')
  return data.data
}

export async function addGridCommunity(name: string): Promise<void> {
  await api.post('/grid-members/communities', null, { params: { name } })
}

export async function deleteGridCommunity(id: number): Promise<void> {
  await api.delete(`/grid-members/communities/${id}`)
}

export async function updateGridCommunityDetails(
  id: number,
  name: string,
  aliases: string[],
  policeOfficerIds: number[],
  areaId: number,
): Promise<{
  name: string
  aliases: string[]
  police_officers: string[]
  matched_visit_rows: number
}> {
  const { data } = await api.put(`/grid-members/communities/${id}/aliases`, {
    name,
    aliases,
    police_officer_ids: policeOfficerIds,
    area_id: areaId,
  })
  return data
}

export async function importCommunitiesFromData(): Promise<{ new_count: number; new_names: string[] }> {
  const { data } = await api.post('/grid-members/communities/import-from-data')
  return data
}

export type CreateGridMemberPayload = GridMemberPayload & {
  name: string
  account_mode: 'existing' | 'create'
  existing_user_id?: number | null
  username?: string
  password?: string
}

export async function createGridMember(payload: CreateGridMemberPayload): Promise<void> {
  await api.post('/grid-members', payload)
}

export async function updateGridMember(id: number, payload: GridMemberPayload): Promise<void> {
  await api.put(`/grid-members/${id}`, payload)
}

export async function updateGridMemberLeave(
  id: number,
  payload: {
    action: 'temporary' | 'long_term' | 'clear'
    leave_start_date?: string | null
    leave_end_date?: string | null
    leave_reason?: string
  },
): Promise<void> {
  await api.put(`/grid-members/${id}/leave`, payload)
}

export async function deleteGridMember(id: number): Promise<void> {
  await api.delete(`/grid-members/${id}`)
}

export async function extractGridMembers(): Promise<{ new_count: number; new_names: string[]; communities: string[] }> {
  const { data } = await api.post('/grid-members/extract')
  return data
}

export function exportGridMembersUrl(): string {
  return '/api/grid-members/export'
}

export type WeekendDutyDay = 'saturday' | 'sunday'

export interface WeekendDutyMember {
  id: number
  name: string
  community: string
  position: string
  assignment: WeekendDutyDay | null
  recorded: boolean
  previous_assignment: WeekendDutyDay | null
  unavailable_days: WeekendDutyDay[]
  exempt: boolean
  absence_reason: string
}

export interface WeekendDutyBoard {
  week_start: string
  saturday: string
  sunday: string
  positions: string[]
  members: WeekendDutyMember[]
  complete: boolean
  unassigned_count: number
}

export interface AttendanceScheduleStatus {
  start_date: string
  end_date: string
  complete: boolean
  missing_week_starts: string[]
}

export interface AttendanceHistoryItem {
  id: number
  member_id: number
  member_name: string
  absence_type: 'temporary_leave' | 'long_term_leave'
  start_date: string
  end_date: string | null
  reason: string
  source: string
  is_active: boolean
  created_at: string
}

export async function getAttendanceHistory(params?: {
  member_id?: number
  page?: number
  page_size?: number
}): Promise<{
  data: AttendanceHistoryItem[]
  total: number
  page: number
  page_size: number
}> {
  const { data } = await api.get('/personnel/attendance/history', { params })
  return data
}

export async function getWeekendDuty(
  weekStart: string,
): Promise<WeekendDutyBoard> {
  const { data } = await api.get('/personnel/attendance/weekend-duty', {
    params: { week_start: weekStart },
  })
  return data
}

export async function getAttendanceScheduleStatus(
  startDate: string,
  endDate: string,
): Promise<AttendanceScheduleStatus> {
  const { data } = await api.get('/personnel/attendance/status', {
    params: {
      start_date: startDate,
      end_date: endDate,
    },
  })
  return data
}

export async function saveWeekendDuty(
  weekStart: string,
  assignments: Array<{
    member_id: number
    duty_day: WeekendDutyDay | null
  }>,
): Promise<WeekendDutyBoard> {
  const { data } = await api.put('/personnel/attendance/weekend-duty', {
    week_start: weekStart,
    assignments,
  })
  return data
}

// ---- Visit detail imports ----
export interface VisitCoverage {
  scope_message?: string
  start_date: string | null
  end_date: string | null
  total_records: number
  rated_records: number
  unrated_records: number
  data_days: number
  missing_date_count: number
  missing_dates: string[]
  last_import_at: string | null
  last_detail_import_at: string | null
  last_rating_import_at: string | null
}

export interface VisitImportIssue {
  id: number
  severity: 'error' | 'warning'
  code: string
  row_number: number
  message: string
  row_preview: Record<string, string>
}

export interface VisitIssuePage {
  data: VisitImportIssue[]
  total: number
  page: number
  page_size: number
}

export interface VisitImportResult {
  batch_id: number
  import_type: 'detail' | 'rating'
  status: 'success' | 'partial' | 'failed' | 'duplicate'
  duplicate_file: boolean
  file_start_date: string | null
  file_end_date: string | null
  overlap_start_date: string | null
  overlap_end_date: string | null
  inserted_rows: number
  updated_rows: number
  unchanged_rows: number
  ignored_rows: number
  matched_rows?: number
  unmatched_rows?: number
  ambiguous_rows?: number
  error_count: number
  warning_count: number
  message: string
  coverage: VisitCoverage
  issues: VisitIssuePage
}

export interface VisitSummaryTable {
  columns: string[]
  data: Array<Record<string, string | number | null>>
  summary: Record<string, string | number | null>
}

export type VisitSummaryCategory = 'rental' | 'self_owned'

export interface VisitSummaryReport {
  scope_message?: string
  category: VisitSummaryCategory
  category_label: string
  start_date: string
  end_date: string
  attendance: {
    complete: boolean
    person_days: number
    missing_week_starts: string[]
    history_started_on: string | null
    legacy_history_incomplete: boolean
    worked_while_off: number
    unknown_participant_days: number
  }
  overview: {
    visit_records: number
    participant_count: number
    person_days: number
    community_count: number
    added_count: number
    changed_count: number
    cancelled_count: number
    total_changes: number
    rated_records: number
    unrated_records: number
    rating_rate: number
  }
  inspector: VisitSummaryTable
  community: VisitSummaryTable
}

export async function getVisitCoverage(): Promise<VisitCoverage> {
  const { data } = await api.get('/visits/coverage')
  return data
}

export async function getVisitSummary(
  startDate: string,
  endDate: string,
  category: VisitSummaryCategory = 'rental',
): Promise<VisitSummaryReport> {
  const { data } = await api.get('/visits/summary', {
    ...activeRequest,
    params: {
      start_date: startDate,
      end_date: endDate,
      category,
    },
  })
  return data
}

export async function uploadVisitDetail(file: File): Promise<VisitImportResult> {
  const formData = new FormData()
  formData.append('file', file)
  const { data } = await api.post('/visits/imports/detail', formData, {
    timeout: 300000,
  })
  return data
}

export async function uploadStarRating(file: File): Promise<VisitImportResult> {
  const formData = new FormData()
  formData.append('file', file)
  const { data } = await api.post('/visits/imports/rating', formData, {
    timeout: 300000,
  })
  return data
}

export async function getVisitImportIssues(
  batchId: number,
  page: number,
  pageSize = 50,
): Promise<VisitIssuePage> {
  const { data } = await api.get(`/visits/imports/${batchId}/issues`, {
    params: { page, page_size: pageSize },
  })
  return data
}

export async function updateGridCommunityStatus(
  id: number,
  isActive: boolean,
): Promise<{ message: string; is_active: boolean }> {
  const { data } = await api.patch(`/grid-members/communities/${id}/status`, {
    is_active: isActive,
  })
  return data
}

// ---- 公安全链条预处理与地址库 ----
export interface PoliceCommunityOption {
  id: number
  name: string
  enabled: boolean
  aliases: string[]
}

export interface PoliceAddressEntry {
  id: number
  name: string
  detail_address: string
  address_type: 'community' | 'apartment' | 'other'
  pattern: string
  community_id: number
  community_name: string
  aliases: string[]
  sources: string[]
  enabled: boolean
  created_at: string | null
  updated_at: string | null
}

export type PoliceAddressPayload = Omit<
  PoliceAddressEntry,
  'id' | 'community_name' | 'sources' | 'created_at' | 'updated_at'
>

export interface PoliceAddressImportResult {
  status: 'preview' | 'success' | 'partial' | 'duplicate'
  total?: number
  accepted?: Array<Record<string, unknown>>
  conflicts?: Array<Record<string, string | number>>
  create_count?: number
  merge_count?: number
  created_count?: number
  merged_count?: number
  imported_count?: number
  conflict_count?: number
  import_id?: number
  message?: string
}

export interface PoliceDispatchCounts {
  total: number
  pending_review: number
  reviewed: number
  no_registration: number
  transfer: number
  dispatch: number
  balanced: number
  duplicate: number
  abnormal: number
  pending_publish: number
  published: number
  retryable: number
  needs_reconciliation: number
  conflict: number
  cache_pending: number
}

export interface PoliceDispatchBatch {
  id: number
  file_name: string
  sheet_name: string
  status: 'reviewing' | 'ready_to_publish' | 'publishing' | 'reconciling' | 'completed'
  total_count: number
  counts: PoliceDispatchCounts
  reviewed_count: number
  first_publish_date: string | null
  last_error: string
  created_at: string
  updated_at: string
  imported_by: string
  community_distribution: Array<{
    community_id: number
    community_name: string
    count: number
  }>
}

export interface PoliceDispatchTask {
  id: number
  batch_id: number
  source_row: number
  source_name: string
  person_name: string
  identity_number: string
  phone: string
  original_address: string
  created_time: string
  transfer_note: string
  duplicate_group_key: string
  duplicate_kind: 'exact' | 'conflict' | ''
  suggested_action: 'dispatch' | 'no_registration' | 'transfer' | 'manual'
  suggested_community_id: number | null
  suggested_community_name: string
  suggestion_reason: string
  allocation_mode: 'matched' | 'balanced' | 'conflict' | ''
  final_action: 'dispatch' | 'no_registration' | 'transfer' | 'duplicate_exclude' | ''
  final_community_id: number | null
  final_community_name: string
  review_note: string
  reviewer_name: string
  reviewed_at: string | null
  version: number
  task_status: 'pending_review' | 'pending_publish' | 'publish_failed' | 'completed'
  publish_status: string
  publish_error: string
  raw_values: Record<string, string>
  field_roles: Record<string, string>
  linked_source_id: number | null
  linked_row_hash: string
  conflict_values: Record<string, string>
  requested_values: Record<string, string>
  conflict_diff: Array<{ field: string; platform: string; tencent: string }>
  cache_pending: boolean
}

export async function listPoliceAddresses(params?: {
  keyword?: string
  enabled?: boolean
}): Promise<{ data: PoliceAddressEntry[]; total: number; communities: PoliceCommunityOption[] }> {
  const { data } = await api.get('/police-dispatch/addresses', { params })
  return data
}

export async function createPoliceAddress(payload: PoliceAddressPayload): Promise<void> {
  await api.post('/police-dispatch/addresses', payload)
}

export async function updatePoliceAddress(id: number, payload: PoliceAddressPayload): Promise<void> {
  await api.put(`/police-dispatch/addresses/${id}`, payload)
}

export async function disablePoliceAddress(id: number): Promise<void> {
  await api.delete(`/police-dispatch/addresses/${id}`)
}

export async function importPoliceAddresses(
  file: File,
  importKind: 'community' | 'apartment',
  commit = false,
): Promise<PoliceAddressImportResult> {
  const form = new FormData()
  form.append('file', file)
  const { data } = await api.post('/police-dispatch/addresses/import', form, {
    params: { import_kind: importKind, commit },
    timeout: 300000,
  })
  return data
}

export async function uploadPoliceDispatchBatch(file: File): Promise<{
  status: 'success' | 'duplicate'
  message: string
  batch: PoliceDispatchBatch
}> {
  const form = new FormData()
  form.append('file', file)
  const { data } = await api.post('/police-dispatch/batches', form, { timeout: 300000 })
  return data
}

export async function listPoliceDispatchBatches(params?: {
  file_name?: string
  upload_date?: string
  status?: string
  page?: number
  page_size?: number
}): Promise<{ data: PoliceDispatchBatch[]; total: number; page: number; page_size: number }> {
  const { data } = await api.get('/police-dispatch/batches', { params })
  return data
}

export async function getPoliceDispatchBatch(id: number): Promise<{
  batch: PoliceDispatchBatch
  communities: PoliceCommunityOption[]
}> {
  const { data } = await api.get(`/police-dispatch/batches/${id}`)
  return data
}

export async function getPoliceDispatchWorkbench(): Promise<{
  active_batch: PoliceDispatchBatch | null
  batches: PoliceDispatchBatch[]
  communities: PoliceCommunityOption[]
}> {
  const { data } = await api.get('/police-dispatch/workbench/home', activeRequest)
  return data
}

export async function listPoliceDispatchTasks(params: {
  batch_id: number
  status?: string
  category?: string
  keyword?: string
  page?: number
  page_size?: number
}): Promise<{ data: PoliceDispatchTask[]; total: number; page: number; page_size: number }> {
  const { data } = await api.post('/police-dispatch/tasks/search', params, activeRequest)
  return data
}

export async function getPoliceDispatchTask(id: number): Promise<{
  task: PoliceDispatchTask
  duplicates: PoliceDispatchTask[]
  duplicate_differences: Array<{
    task_id: number
    source_row: number
    fields: Array<{ field: string; value: string }>
  }>
  communities: PoliceCommunityOption[]
}> {
  const { data } = await api.get(`/police-dispatch/tasks/${id}`, activeRequest)
  return data
}

export async function updatePoliceDispatchBusinessFields(
  id: number,
  payload: { expected_version: number; fields: Record<string, string> },
): Promise<void> {
  await api.patch(`/police-dispatch/tasks/${id}/business-fields`, payload)
}

export async function resolvePoliceDispatchConflict(
  id: number,
  payload: {
    expected_version: number
    strategy: 'adopt_tencent' | 'overwrite_tencent'
    expected_row_hash: string
    confirmation?: string
  },
): Promise<{ message: string; cache_pending: boolean }> {
  const { data } = await api.post(`/police-dispatch/tasks/${id}/resolve-conflict`, payload)
  return data
}

export async function reviewPoliceDispatchTask(
  id: number,
  payload: {
    expected_version: number
    final_action: Exclude<PoliceDispatchTask['final_action'], ''>
    final_community_id?: number | null
    review_note?: string
  },
): Promise<void> {
  await api.patch(`/police-dispatch/tasks/${id}`, payload)
}

export async function bulkReviewPoliceDispatchTasks(payload: {
  tasks: Array<{ id: number; version: number }>
  mode: 'accept_suggestion' | 'set_action'
  final_action?: Exclude<PoliceDispatchTask['final_action'], ''>
  final_community_id?: number | null
  review_note?: string
}): Promise<void> {
  await api.post('/police-dispatch/tasks/bulk-review', payload)
}

export async function publishPoliceDispatchBatch(id: number): Promise<{
  message: string
  success_count: number
  failed_count: number
}> {
  const { data } = await api.post(`/police-dispatch/batches/${id}/publish`)
  return data
}

export function policeDispatchFeedbackUrl(id: number): string {
  return `/api/police-dispatch/batches/${id}/feedback.xlsx`
}

// ---- System Config ----
export async function getSystemConfig(): Promise<Record<string, string>> {
  const { data } = await api.get('/system/config')
  return data.data
}

export async function updateSystemConfig(config: Record<string, string>): Promise<void> {
  await api.put('/system/config', config)
}

// 时间格式化工具（UTC → 指定时区）
export function formatUTCTime(utcStr: string | null | undefined, timezone: string = 'Asia/Shanghai'): string {
  if (!utcStr) return '-'
  try {
    const date = new Date(utcStr)
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: timezone,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hour12: false,
    }).format(date).replace(/\//g, '-')
  } catch { return utcStr }
}

export function formatDateInTimezone(date: Date = new Date(), timezone: string = 'Asia/Shanghai'): string {
  try {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: timezone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).formatToParts(date)
    const values = Object.fromEntries(parts.map(part => [part.type, part.value]))
    return `${values.year}-${values.month}-${values.day}`
  } catch {
    return new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Shanghai',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(date)
  }
}

// ---- Auth ----
export async function getAuthStatus(): Promise<OAuthStatus> {
  const { data } = await api.get('/auth/status')
  return data
}

export async function saveOAuth(payload: OAuthConfig): Promise<void> {
  await api.post('/auth/oauth', payload)
}

export async function testOAuth(payload: OAuthConfig): Promise<{ valid: boolean; message: string }> {
  const { data } = await api.post('/auth/oauth/test', payload)
  return data
}
