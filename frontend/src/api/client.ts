import axios from 'axios'
import type {
  StatsResponse, StatsItem, AppNotification,
  OpsOverview, OpsDatabase, BackupSchedule,
  BackupJob, AuditActionOption, AuditEvent, User, UserPreferences, ReportColumnMode,
  WorkLogDraft, WorkLogDraftSummary, WorkLogMissingItem, WorkLogSchema,
  PublicProfile, PublicProfileSummary,
  VisitSourceRun,
  PresenceHeartbeatResponse, PresenceUsersResponse,
} from '../types'
import { getClientDeviceHeaders } from '../utils/device.ts'
import { resolveApiAssetUrl } from '../utils/apiUrl.ts'

const configuredApiBaseUrl = (import.meta.env?.VITE_API_BASE_URL || '').replace(/\/+$/, '')
const apiBaseUrl = configuredApiBaseUrl || '/api'

const api = axios.create({
  baseURL: apiBaseUrl,
  timeout: 30000,
  withCredentials: true,
})
const webClientVersion = typeof __APP_VERSION__ === 'string'
  ? __APP_VERSION__
  : '0.0.0'
const activeRequest = { headers: { 'X-User-Activity': '1' } }
const passiveRequest = { headers: { 'X-User-Activity': '0' } }
let unauthorizedRedirectStarted = false

function normalizeUserAssets(user: User): User {
  return {
    ...user,
    avatar_url: resolveApiAssetUrl(user.avatar_url, configuredApiBaseUrl),
  }
}

export interface VenueCodeItem {
  id: number
  name: string
  venue_type: string
  address: string
  community_id: number | null
  community_name: string
  status: 'active' | 'inactive'
  created_at?: string | null
  updated_at?: string | null
}

export interface VenueVisitItem {
  id: number
  venue_id: number
  venue_name: string
  name: string
  identity_number: string
  phone: string
  address: string
  submitted_at: string | null
  photo: { mime_type: string; size_bytes: number } | null
}

export async function listVenueCodes(): Promise<{ data: VenueCodeItem[] }> {
  return (await api.get('/venue-codes')).data
}
export async function createVenueCode(payload: Omit<VenueCodeItem, 'id' | 'created_at' | 'updated_at'>): Promise<{ id: number; token: string; url: string }> {
  return (await api.post('/venue-codes', payload)).data
}
export async function updateVenueCode(id: number, payload: Omit<VenueCodeItem, 'id' | 'created_at' | 'updated_at'>): Promise<void> {
  await api.put(`/venue-codes/${id}`, payload)
}
export async function deleteVenueCode(id: number): Promise<void> {
  await api.delete(`/venue-codes/${id}`)
}
export async function rotateVenueCodeToken(id: number): Promise<{ token: string; url: string }> {
  return (await api.post(`/venue-codes/${id}/rotate-token`, {})).data
}
export function resolveVenueCodeQrImageUrl(
  imageUrl: string | null | undefined,
  apiBaseUrl = configuredApiBaseUrl,
): string | undefined {
  return resolveApiAssetUrl(imageUrl, apiBaseUrl) || undefined
}
export async function getVenueCodeQr(id: number): Promise<{ venue: VenueCodeItem; token: string; url: string; image_url?: string }> {
  const data = (await api.get(`/venue-codes/${id}/qrcode`)).data
  return {
    ...data,
    image_url: resolveVenueCodeQrImageUrl(data.image_url),
  }
}
export async function listVenueVisits(params: Record<string, unknown> = {}): Promise<{ data: VenueVisitItem[]; total: number; page: number; page_size: number }> {
  return (await api.get('/venue-visits', { params })).data
}
export async function exportVenueVisits(params: Record<string, unknown> = {}): Promise<Blob> {
  return (await api.get('/venue-visits/export', { params, responseType: 'blob' })).data
}
export async function getPublicVenueInfo(token: string): Promise<{ venue_id: number; name: string; form_token: string }> {
  return (await api.get(`/public/venue-codes/${encodeURIComponent(token)}`)).data
}

export interface MaintenanceStatus {
  enabled: boolean
  active: boolean
  scheduled: boolean
  start_at: string | null
  end_at: string | null
  message: string
  server_time: string
  timezone: string
}

export interface AppBootstrapSummary {
  server_version: string
  timezone: string
}

export interface LoginSessionSummary {
  management_id: string
  device_type: 'desktop' | 'mobile'
}

export interface LoginResponse {
  message: string
  session_refresh_required?: boolean
  session?: LoginSessionSummary
  user: User
}

export interface AuthSessionItem {
  management_id: string
  device_type: 'desktop' | 'mobile'
  client_platform: string
  user_agent_family: string
  created_at: string | null
  last_activity_at: string | null
  expires_at: string | null
  current: boolean
}

export type AdminTaskQueueState =
  | 'queued'
  | 'running'
  | 'retrying'
  | 'success'
  | 'warning'
  | 'failed'
  | 'paused'
  | 'cancelled'

export interface AdminTaskQueueItem {
  id: string
  source: string
  category: string
  title: string
  state: AdminTaskQueueState
  phase: string
  current: number | null
  total: number | null
  progress: number | null
  message: string
  active: boolean
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  updated_at: string | null
  detail_count: number
  attention_count: number
  retry_kind: string | null
}

export interface AdminTaskQueueDetailItem {
  id: number
  state: string
  reference: string
  action: string
  attempt_count: number
  error_code: string
  diagnosis: string
  recommended_action: string
  can_retry: boolean
  retry_kind: 'photo_outbox' | null
  updated_at: string | null
}

export interface AdminTaskQueueDetailResponse {
  source: string
  page: number
  page_size: number
  total: number
  data: AdminTaskQueueDetailItem[]
}

export interface AdminTaskQueueResponse {
  server_time: string
  refresh_after_seconds: number
  active_count: number
  queued_count: number
  running_count: number
  attention_count: number
  items: AdminTaskQueueItem[]
  unavailable_sources: string[]
}

export function resetUnauthorizedRedirectForTests(): void {
  unauthorizedRedirectStarted = false
}

export interface AuthFetchOptions {
  handleUnauthorized?: boolean
  markActivity?: boolean
}

function resolveApiRequest(input: RequestInfo | URL): RequestInfo | URL {
  if (!configuredApiBaseUrl || input instanceof URL) return input
  if (typeof input !== 'string' || /^https?:\/\//i.test(input)) return input
  if (input.startsWith('/api')) return `${configuredApiBaseUrl}${input.slice(4)}`
  return `${configuredApiBaseUrl}${input.startsWith('/') ? input : `/${input}`}`
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

export function handleMaintenance(detail?: unknown): boolean {
  const payload = detail && typeof detail === 'object'
    ? detail as { code?: unknown; message?: unknown }
    : null
  if (payload?.code !== 'maintenance_mode') {
    return false
  }
  if (window.location.pathname.includes('/login') || unauthorizedRedirectStarted) {
    return true
  }
  const message = typeof payload?.message === 'string'
    ? payload.message
    : '平台正在维护中，请稍后再试'
  unauthorizedRedirectStarted = true
  sessionStorage.setItem('auth_exit_reason', JSON.stringify({
    code: 'maintenance_mode',
    message,
  }))
  window.location.href = '/login'
  return true
}

export async function fetchWithAuth(
  input: RequestInfo | URL,
  init: RequestInit = {},
  options: AuthFetchOptions = {},
): Promise<Response> {
  const headers = new Headers(init.headers)
  if (!headers.has('X-Binhu-Client-Platform')) {
    const deviceHeaders = getClientDeviceHeaders()
    headers.set('X-Binhu-Client-Platform', deviceHeaders['X-Binhu-Client-Platform'])
  }
  if (!headers.has('X-Binhu-Device-Id')) {
    headers.set('X-Binhu-Device-Id', getClientDeviceHeaders()['X-Binhu-Device-Id'])
  }
  if (!headers.has('X-Binhu-Client-Version')) {
    headers.set('X-Binhu-Client-Version', webClientVersion)
  }
  const method = (init.method || 'GET').toUpperCase()
  if (
    options.markActivity !== false
    && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)
    && !headers.has('X-User-Activity')
  ) {
    headers.set('X-User-Activity', '1')
  }
  const response = await fetch(resolveApiRequest(input), {
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
  } else if (response.status === 503 && options.handleUnauthorized !== false) {
    const body = await response.clone().json().catch(() => null)
    const detail = body && typeof body === 'object'
      ? (body as { detail?: unknown }).detail
      : null
    handleMaintenance(detail)
  }
  return response
}

export async function fetchAuthenticatedImageBlob(input: RequestInfo | URL): Promise<Blob> {
  const response = await fetchWithAuth(
    input,
    { method: 'GET' },
    { markActivity: false },
  )
  if (!response.ok) {
    throw new Error(`图片读取失败（${response.status}）`)
  }
  const blob = await response.blob()
  if (!blob.type.startsWith('image/')) {
    throw new Error('服务器返回的不是图片')
  }
  return blob
}

api.interceptors.request.use((config) => {
  const deviceHeaders = getClientDeviceHeaders()
  config.headers.set('X-Binhu-Client-Platform', deviceHeaders['X-Binhu-Client-Platform'])
  config.headers.set('X-Binhu-Device-Id', deviceHeaders['X-Binhu-Device-Id'])
  config.headers.set('X-Binhu-Client-Version', webClientVersion)
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
    } else if (error?.response?.status === 503) {
      handleMaintenance(error?.response?.data?.detail)
    }
    return Promise.reject(error)
  }
)

export async function getCurrentUser(): Promise<User> {
  const { data } = await api.get('/auth/me')
  return normalizeUserAssets(data.user)
}

export async function getAuthSessions(): Promise<AuthSessionItem[]> {
  const { data } = await api.get('/auth/sessions')
  return data.sessions || []
}

export async function getAdminTaskQueue(): Promise<AdminTaskQueueResponse> {
  const { data } = await api.get('/admin/task-queue', passiveRequest)
  return data
}

export async function getAdminTaskQueueDetails(
  source: string,
  page = 1,
  pageSize = 20,
): Promise<AdminTaskQueueDetailResponse> {
  const { data } = await api.get(
    `/admin/task-queue/${encodeURIComponent(source)}/details`,
    { ...passiveRequest, params: { page, page_size: pageSize } },
  )
  return data
}

export async function getImportantUnreadAnnouncements(): Promise<AppNotification[]> {
  const { data } = await api.get('/notifications/announcements/important-unread', passiveRequest)
  return data.data || []
}

export async function revokeAuthSession(managementId: string): Promise<void> {
  await api.delete(`/auth/sessions/${encodeURIComponent(managementId)}`)
}

export async function revokeOtherAuthSessions(): Promise<number> {
  const { data } = await api.post('/auth/sessions/revoke-others')
  return Number(data.revoked || 0)
}

export async function revokeAllAuthSessions(): Promise<void> {
  await api.post('/auth/sessions/revoke-all', undefined, {
    headers: { 'X-User-Activity': '1' },
  })
}

export async function uploadAvatar(file: File): Promise<{ avatar_url: string }> {
  const form = new FormData()
  form.append('file', file)
  const { data } = await api.post('/auth/avatar', form)
  return {
    ...data,
    avatar_url: resolveApiAssetUrl(data.avatar_url, configuredApiBaseUrl) || '',
  }
}

export async function getAppBootstrap(): Promise<AppBootstrapSummary> {
  const response = await fetchWithAuth(
    '/api/app/bootstrap',
    { method: 'GET' },
    { handleUnauthorized: false, markActivity: false },
  )
  if (!response.ok) {
    throw new Error('无法读取平台版本')
  }
  return response.json()
}

export async function recordSessionActivity(): Promise<User> {
  const { data } = await api.post('/auth/activity')
  return normalizeUserAssets(data.user)
}

export async function sendPresenceHeartbeat(clientId: string): Promise<PresenceHeartbeatResponse> {
  const { data } = await api.post(
    '/presence/heartbeat',
    { client_id: clientId },
    { headers: { 'X-User-Activity': '0' } },
  )
  return data
}

export async function getPresenceUsers(): Promise<PresenceUsersResponse> {
  const { data } = await api.get('/presence/users')
  return {
    ...data,
    users: (data.users || []).map((user: PresenceUsersResponse['users'][number]) => ({
      ...user,
      avatar_url: resolveApiAssetUrl(user.avatar_url, configuredApiBaseUrl),
    })),
  }
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
}): Promise<{
  data: AuditEvent[]
  total: number
  page: number
  page_size: number
  action_options: AuditActionOption[]
}> {
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

export interface WorkLogDailyDetailPreferences {
  rental_target: number
  self_owned_target: number
}

export async function getWorkLogDailyDetailPreferences(): Promise<WorkLogDailyDetailPreferences> {
  const { data } = await api.get('/work-logs/daily-detail/preferences')
  return data
}

export async function exportWorkLogDailyDetail(payload: {
  business_date: string
  rental_target: number
  self_owned_target: number
}): Promise<Blob> {
  const { data } = await api.post(
    '/work-logs/daily-detail/export',
    payload,
    { responseType: 'blob', timeout: 120000 },
  )
  return data
}

// ---- Stats / 日报 ----
export async function getReportTypes(): Promise<{ data: string[]; implemented: string[] }> {
  const { data } = await api.get('/stats/types')
  return data
}

export async function recordXlsxExport(payload: {
  export_type: 'online_summary' | 'visit_summary' | 'code_summary'
  start_date: string
  end_date: string
  summary_type: string
  inspector_rows: number
  community_rows: number
}): Promise<void> {
  await api.post('/exports/xlsx', payload)
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

export type OnlineOverviewCategory = 'carryover' | 'new' | 'changed' | 'pending' | 'completed'

export interface OnlineOverviewDetailItem {
  parser_type: string
  row_key: string
  community: string
  inspector: string
  state: 'unchecked' | 'checked' | 'completed' | string
  first_activity_date: string
  first_dispatch_date?: string
  last_activity_date: string
  reason: string
  summary: {
    title: string
    identity_number: string
    phone: string
    source: string
    address: string
    date: string
    result: string
  }
  values: Record<string, string>
}

export interface OnlineOverviewDetails {
  category: OnlineOverviewCategory
  category_label: string
  total: number
  page: number
  page_size: number
  data: OnlineOverviewDetailItem[]
}

export async function getOnlineDataOverview(
  startDate: string,
  endDate: string,
  parserType: string,
  filters?: { scope?: 'permission' | 'responsibility'; community?: string },
): Promise<OnlineDataOverview> {
  const { data } = await api.get('/stats/overview', {
    params: {
      start_date: startDate,
      end_date: endDate,
      parser_type: parserType,
      ...filters,
    },
  })
  return data
}

export async function getOnlineDataOverviewDetails(params: {
  start_date: string
  end_date: string
  parser_type: string
  category: OnlineOverviewCategory
  page?: number
  page_size?: number
  scope?: 'permission' | 'responsibility'
  community?: string
}): Promise<OnlineOverviewDetails> {
  const { data } = await api.get('/stats/overview/details', {
    ...activeRequest,
    params,
  })
  return data
}

export async function saveUserPreferences(payload: UserPreferences): Promise<User> {
  const { data } = await api.put('/auth/preferences', payload)
  return normalizeUserAssets(data.user)
}

export async function getReport(
  date: string,
  parser_type?: string,
  columnMode?: ReportColumnMode,
  filters?: { scope?: 'permission' | 'responsibility'; community?: string },
): Promise<any> {
  const { data } = await api.get('/stats/report', {
    params: {
      report_date: date,
      parser_type: parser_type || '全链条',
      column_mode: columnMode,
      ...filters,
    },
  })
  return data
}

export async function getReportRange(
  startDate: string,
  endDate: string,
  parserType: string,
  columnMode?: ReportColumnMode,
  filters?: { scope?: 'permission' | 'responsibility'; community?: string },
): Promise<any> {
  const { data } = await api.get('/stats/report_range', {
    params: {
      start_date: startDate,
      end_date: endDate,
      parser_type: parserType,
      column_mode: columnMode,
      ...filters,
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
  __inspector_mismatch?: boolean
}

export interface QueryDependentOptions {
  community_aliases: Record<string, string>
  inspectors_by_community: Record<string, string[]>
  fallback_inspectors: string[]
  community_column: string
  inspector_column: string
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
  data_source_mode?: 'local' | 'tencent'
  writeback_enabled: boolean
  can_add: boolean
  required_fields: string[]
  pending_count: number
  data_version?: string
  scope_message?: string
  row_manage_message?: string
  dependent_options?: QueryDependentOptions
}

export async function getQueryDataVersion(type: string): Promise<{ data_version: string }> {
  const { data } = await api.get(`/query/${type}/version`, activeRequest)
  return data
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
  payload: {
    column: string
    value: string
    expected_revision: number
    explicit_text_edit?: boolean
  },
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
export type MobileTaskScope = 'mine' | 'community' | 'all'
export type MobileTaskStatus =
  | 'pending'
  | 'unchecked'
  | 'checked'
  | 'review'
  | 'registration_review'
  | 'completed'
  | 'all'
export type MobileTaskState = 'unchecked' | 'checked' | 'completed'
export type MobileTaskSyncState = '' | 'pending' | 'retry' | 'conflict'
export type MobileTaskReviewStage =
  | 'all'
  | 'waiting_analysis'
  | 'analyzed'
  | 'initial_pending'
  | 'initial_extension'
  | 'deep_pending'
  | 'deep_extension'
  | 'final_unverifiable'
  | 'source_exception'

export interface MobileTaskReviewEvent {
  stage: string
  action: string
  outcome: string
  text?: string
  actor_user_id?: number | null
  automatic: boolean
  safe_reason_code: string
  created_at: string | null
}

export interface MobileTaskReviewFlow {
  id: number
  parser_type: string
  row_key: string
  cycle_no: number
  source_id: number | null
  source_revision: number
  source_row_hash: string
  state: string
  state_label: string
  flow_version: number
  review_due_date: string | null
  original_deadline: string
  previous_deadline: string
  feedback_submitted: boolean
  safe_reason_code: string
  last_action_at: string | null
  resolved_at: string | null
  finalized_at: string | null
  archived_at: string | null
  updated_at: string | null
  events?: MobileTaskReviewEvent[]
}
export type MobileTaskPriority =
  | 'all'
  | 'analyzed'
  | 'source_exception'
  | 'pending_sync'
  | 'ordinary'
  | 'waiting_analysis'
  | 'completed'
export type MobileTaskSort =
  | 'priority'
  | 'address_asc'
  | 'identity_asc'
  | 'updated_desc'
  | 'updated_asc'

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
  admin_mode: boolean
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
  current_address: string
  original_address: string
  deadline: string
  date: string
  result: string
  analysis: string
  note: string
  secondary_feedback: string
  registration_status: string
}

export interface MobileTaskItem {
  task_key: string
  row_key: string
  parser_type: string
  summary: MobileTaskSummaryFields
  community: string
  inspector: string
  state: MobileTaskState
  needs_review: boolean
  review_stage: Exclude<MobileTaskReviewStage, 'all'> | ''
  review_flow?: MobileTaskReviewFlow | null
  photo_fetched: boolean
  source_count: number
  conflict: boolean
  pending_sync: boolean
  sync_state: MobileTaskSyncState
  priority: Exclude<MobileTaskPriority, 'all'>
  watch_marks: MobileTaskWatchMark[]
  first_dispatch_at: string | null
  qmf_status: MobileTaskQmfStatus | null
  residence_status?: MobileTaskResidenceStatus | null
  registration_link?: MobileTaskRegistrationLink | null
}

export type ResidenceRegistrationState =
  | 'pending'
  | 'querying'
  | 'registered'
  | 'first_registration'
  | 'error'
  | 'stale'

export interface MobileTaskResidenceStatus {
  state: ResidenceRegistrationState
  checked_at: string | null
  last_attempt_at: string | null
  error_code: string
  duration_ms?: number | null
}

export interface MobileTaskRegistrationProperty {
  id: number
  natural_address: string
  building: string
  room: string
  normalized_address: string
  status: string
  version: number
  community_id: number | null
  community_name: string
}

export interface MobileTaskRegistrationLink {
  source_id: number | null
  property_id: number | null
  property_version: number | null
  status: string
  reason_code: string
  reason: string
  match_count: number
  selected_at: string | null
  confirmed_at: string | null
  archive_available_at?: string | null
  archive_ready?: boolean
  manual_confirmed_at?: string | null
  manual_reason: string
  property: MobileTaskRegistrationProperty | null
}

export interface ResidenceRegistrationDetail {
  state: 'registered'
  registered_address: string
  household_address: string
  birth_date: string
  age: number | null
  ethnicity: string
  registration_status: 'active' | 'cancelled' | 'unknown'
  registration_status_text: string
  updated_at: string
  photo_data_url: string
  photo_state: 'available' | 'missing' | 'error'
  photo_error_code: string
}

export type QmfFeedbackState =
  | 'not_scanned'
  | 'stale'
  | 'pending'
  | 'completed_match'
  | 'completed_mismatch'
  | 'not_found'
  | 'non_jurisdiction'
  | 'error'

export interface MobileTaskQmfStatus {
  state: QmfFeedbackState
  platform_result: string
  feedback_result: string
  checked_at: string
  origin: 'binhu_automatic' | 'legacy_manual_or_other' | ''
  error_code: string
  last_scanned_at: string | null
}

export interface MobileTaskWatchMark {
  category_id: number
  name: string
  color: string
  alert_level: 'normal' | 'notice' | 'warning' | 'critical'
  assignment_status: string
  source_type: string
  snapshot_status: string
  snapshot_reason: string
}

export interface MobileTaskFilterOption {
  value: string
  label: string
  count: number
}

export interface MobileTaskFacets {
  total: number
  priority_counts: Record<Exclude<MobileTaskPriority, 'all'>, number>
  review_stage_counts?: Record<'initial_pending' | 'initial_extension' | 'deep_pending' | 'deep_extension', number>
  status_counts: Record<MobileTaskState, number>
  registration_review_count: number
  qmf_feedback_counts: Record<QmfFeedbackState, number>
}

export interface MobileTaskSource {
  id: number
  physical_row: number
  source_available: boolean
  values: Record<string, string>
  cell_meta: Record<string, Omit<QueryColumnMeta, 'field'>>
  revision: number
  row_hash: string
  editable_fields: string[]
  state: MobileTaskState
  needs_review: boolean
  review_stage: Exclude<MobileTaskReviewStage, 'all'> | ''
  review_flow?: MobileTaskReviewFlow | null
  sync_state: MobileTaskSyncState
  sync_fields: Array<{
    field: string
    platform_value: string
    tencent_value: string | null
    status: Exclude<MobileTaskSyncState, ''> | 'processing'
    error_code: string
  }>
}

export interface MobileTaskDetailData {
  task: MobileTaskItem
  qmf_status?: MobileTaskQmfStatus | null
  residence_status?: MobileTaskResidenceStatus | null
  registration_link?: MobileTaskRegistrationLink | null
  registration_manual_confirm_allowed?: boolean
  workflow: {
    label: string
    result_field: string
    phone_fields: string[]
    title_fields: string[]
    address_fields: string[]
    date_fields: string[]
    identity_fields: string[]
    source_fields: string[]
    secondary_fields: string[]
    extra_edit_fields?: string[]
    analysis_fields: string[]
    columns: string[]
  }
  writeback_enabled: boolean
  analysis_mode?: boolean
  dependency_blocked?: boolean
  dependency_message?: string
  photo_requests: Array<{
    ticket_id: number
    ticket_no: string
    attachments: Array<{
      file_id: string
      original_name: string
      mime_type: string
      size_bytes: number
    }>
  }>
  qmf_preview?: {
    visible: boolean
    enabled: boolean
    reason: string
  }
  qmf_registration?: {
    visible: boolean
    enabled: boolean
    reason: string
    latest_run?: QmfRegistrationRun | null
  }
  qmf_feedback?: {
    run_id: number
    status: 'succeeded'
    completed_at: string | null
    tencent_marker_status: QmfTencentMarkerStatus
  } | null
  sources: MobileTaskSource[]
  data_source_mode?: 'local' | 'tencent'
}

export type TaskGraphNodeStatus = 'ready' | 'blocked' | 'in_progress' | 'completed' | 'cancelled' | 'source_missing' | 'archived'
export type TaskGraphAccessMode = 'editable' | 'readonly' | 'blocked'

export interface TaskGraphNode {
  id: string
  task_type: 'online_check' | 'analysis'
  category: string
  parser_type: string
  row_key: string
  title: string
  community: string
  owner: string
  status: TaskGraphNodeStatus
  access_mode: TaskGraphAccessMode
  relationship: 'owned' | 'predecessor' | 'successor'
  description: string
  completed_at: string | null
  archived_at: string | null
  sync_warning: boolean
  open_path: string
}

export interface TaskGraphEdge {
  id: string
  source: string
  target: string
  state: 'active' | 'satisfied' | 'cancelled'
  reason_code: string
  system: true
  deletable: false
}

export interface TaskGraphSearchResponse {
  enabled: boolean
  nodes: TaskGraphNode[]
  edges: TaskGraphEdge[]
  facets: { total?: number; view?: string; owner?: string }
  next_cursors: Record<string, number>
}

export interface TaskGraphPreview {
  projection_rows: number
  unable_to_verify: number
  analyzed: number
  historical_analysis: number
  eligible_chains: number
  blank_inspector: number
  unmatched_inspector: number
}

export interface QmfPreviewResult {
  mode: 'read_only'
  can_submit: false
  platform_task: {
    parser_type: string
    row_key: string
    source_id: number
    name: string
    identity_number: string
    phone: string
    address: string
    community: string
    result: string
  }
  upstream_task: {
    task_id: string
    record_id: string
    name: string
    identity_number: string
    phone: string
    address: string
    police_station: string
    community: string
    community_code: string
    check_status: string
    check_status_text: string
    dispatch_time: string
  }
  person: {
    name: string
    identity_number: string
    phone: string
    current_address: string
    household_address: string
    gender: string
    gender_code: string
    birth_date: string
    birth_date_derived: boolean
    nation: string
    nation_code: string
    education: string
    education_code: string
    marital_status: string
    marital_status_code: string
    community_code: string
    residence_type: string
    residence_method: string
    residence_reason: string
    active_status: string
  } | null
  operator: {
    username: string
    name: string
    station_code: string
    station_name: string
  }
  photo: {
    mime_type: string
    size_bytes: number
    sha256: string
    data_base64: string
  } | null
  destination?: {
    community: string
    community_code: string
    area_code: string
    area_name: string
  }
  checks: Record<string, boolean>
  planned_write_steps: Array<{ key: string; label: string; enabled: boolean }>
  planned_changes: Array<{ key: string; label: string; detail: string }>
  warnings: string[]
}

export type QmfLegacyStatusState =
  | 'pending'
  | 'completed_match'
  | 'completed_mismatch'
  | 'not_found'
  | 'ambiguous'
  | 'station_mismatch'
  | 'non_jurisdiction'
  | 'unknown_result'
  | 'unavailable'

export interface QmfLegacyStatus {
  state: QmfLegacyStatusState
  result: string
  result_text: string
  checked_at: string
  station: string
  matches_platform_result: boolean | null
  origin: 'legacy_manual_or_other' | 'binhu_automatic'
  reason: string
}

export type QmfRegistrationRunStatus =
  | 'prepared'
  | 'executing'
  | 'succeeded'
  | 'failed'
  | 'uncertain'
  | 'expired'
  | 'superseded'

export type QmfRegistrationStepStatus =
  | 'pending'
  | 'sending'
  | 'succeeded'
  | 'failed'
  | 'uncertain'

export type QmfTencentMarkerStatus =
  | 'not_started'
  | 'writing'
  | 'succeeded'
  | 'pending'
  | 'conflict'
  | 'failed'

export interface QmfRegistrationRun {
  id: number
  parser_type: string
  source_id: number
  expected_revision: number
  status: QmfRegistrationRunStatus
  steps: Array<{
    key: string
    label: string
    status: QmfRegistrationStepStatus
    result_code: string
    started_at: string | null
    finished_at: string | null
  }>
  result_code: string
  photo: {
    sha256: string
    mime_type: string
    size_bytes: number
  }
  tencent_marker_status: QmfTencentMarkerStatus
  tencent_marker_error: string
  prepared_at: string | null
  expires_at: string | null
  execution_started_at: string | null
  completed_at: string | null
  created_at: string | null
  updated_at: string | null
  can_execute: boolean
  can_reprepare: boolean
  can_retry_marker: boolean
}

export interface QmfPrepareResult extends Omit<
  QmfPreviewResult,
  'mode' | 'can_submit' | 'planned_write_steps'
> {
  mode: 'prepared'
  can_submit: true
  planned_write_steps: Array<{ key: string; label: string; enabled: true }>
  run: QmfRegistrationRun
}

export interface QmfConfig {
  registration_enabled: boolean
  api_base_url: string
  login_host: string
  login_port: number
  source_username: string
  source_password_configured: boolean
  source_imei: string
  source_machine_uid: string
  expected_station_code: string
  expected_station_name: string
  timeout_seconds: number
  session_max_seconds: number
  status_scan_enabled: boolean
  status_scan_time: string
  configured: boolean
  registration_configured: boolean
  database_keys: string[]
}

export interface QmfConfigUpdate {
  registration_enabled: boolean
  api_base_url: string
  login_host: string
  login_port: number
  source_username: string
  source_password?: string
  source_imei: string
  source_machine_uid: string
  expected_station_code: string
  expected_station_name: string
  timeout_seconds: number
  session_max_seconds: number
  status_scan_enabled: boolean
  status_scan_time: string
}

export interface ResidencePlatformConfig {
  enabled: boolean
  base_url: string
  password_configured: boolean
  mac_service_url: string
  timeout_seconds: number
  full_scan_interval_minutes: number
  credentials_configured: boolean
  session_ready: boolean
  account_mode: 'community_code_suffix_00'
  login_mode: 'automatic_hidden_challenge'
  community_account_count: number
  active_session_count: number
}

export interface ResidencePlatformConfigUpdate {
  enabled: boolean
  base_url: string
  password?: string
  mac_service_url: string
  timeout_seconds: number
  full_scan_interval_minutes: number
}

export type QmfStatusScanRunStatus = 'queued' | 'running' | 'completed' | 'partial' | 'failed'

export interface QmfStatusScanRun {
  id: number
  trigger_source: 'manual' | 'scheduled'
  scan_mode: 'full' | 'incremental'
  status: QmfStatusScanRunStatus
  concurrency: number
  total_count: number
  processed_count: number
  match_count: number
  mismatch_count: number
  pending_count: number
  not_found_count: number
  non_jurisdiction_count: number
  error_count: number
  requested_by: number | null
  error_code: string
  started_at: string | null
  finished_at: string | null
  created_at: string | null
  updated_at: string | null
  failures: Array<{ code: string; count: number }>
}

export interface MobileTaskInlineEditorItem {
  available: boolean
  reason?: string
  detail?: MobileTaskDetailData
}

export interface MobileTaskInlineEditorsData {
  analysis_mode: boolean
  items: Record<string, MobileTaskInlineEditorItem>
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

export interface MobileTaskSearchParams {
  parser_type: string
  scope: MobileTaskScope
  status: MobileTaskStatus
  review_stage?: MobileTaskReviewStage
  communities?: string[]
  inspectors?: string[]
  watch_categories?: number[]
  qmf_feedback_states?: QmfFeedbackState[]
  priority?: MobileTaskPriority
  sort?: MobileTaskSort
  keyword?: string
  page?: number
  page_size?: number
}

export interface MobileTaskAnalysisSearchParams {
  parser_types: string[]
  scope: MobileTaskScope
  review_stage?: MobileTaskReviewStage
  communities?: string[]
  inspectors?: string[]
  watch_categories?: number[]
  sort?: MobileTaskSort
  keyword?: string
  page?: number
  page_size?: number
}

function mobileTaskSearchPayload(params: MobileTaskSearchParams) {
  return {
    scope: params.scope,
    status: params.status,
    review_stage: params.review_stage || 'all',
    communities: params.communities || [],
    inspectors: params.inspectors || [],
    watch_categories: params.watch_categories || [],
    qmf_feedback_states: params.qmf_feedback_states || [],
    priority: params.priority || 'all',
    sort: params.sort || 'priority',
    keyword: params.keyword || '',
    page: params.page || 1,
    page_size: params.page_size || 20,
  }
}

export async function listMobileTasks(
  params: MobileTaskSearchParams,
  options: { passive?: boolean } = {},
): Promise<{
  data: MobileTaskItem[]
  total: number
  page: number
  page_size: number
  source_ready: boolean
  message: string
  facets: MobileTaskFacets
  priority_labels: Record<Exclude<MobileTaskPriority, 'all'>, string>
  filters: {
    scope: MobileTaskScope
    status: MobileTaskStatus
    review_stage: MobileTaskReviewStage
    communities: string[]
    inspectors: string[]
    watch_categories: number[]
    qmf_feedback_states: QmfFeedbackState[]
    priority: MobileTaskPriority
    sort: MobileTaskSort
    keyword_present: boolean
  }
}> {
  const { data } = await api.post(
    `/mobile-tasks/${encodeURIComponent(params.parser_type)}/search`,
    mobileTaskSearchPayload(params),
    options.passive ? passiveRequest : activeRequest,
  )
  return data
}

export async function listMobileTaskAnalysis(
  params: MobileTaskAnalysisSearchParams,
  options: { passive?: boolean } = {},
): Promise<Awaited<ReturnType<typeof listMobileTasks>>> {
  const { data } = await api.post('/mobile-tasks/analysis/search', {
    parser_types: params.parser_types,
    scope: params.scope,
    review_stage: params.review_stage || 'all',
    communities: params.communities || [],
    inspectors: params.inspectors || [],
    watch_categories: params.watch_categories || [],
    sort: params.sort || 'priority',
    keyword: params.keyword || '',
    page: params.page || 1,
    page_size: params.page_size || 20,
  }, options.passive ? passiveRequest : activeRequest)
  return data
}

export async function exportMobileTasks(
  params: MobileTaskSearchParams,
): Promise<Blob> {
  return (await api.post(
    `/mobile-tasks/${encodeURIComponent(params.parser_type)}/export`,
    mobileTaskSearchPayload(params),
    { ...activeRequest, responseType: 'blob', timeout: 300_000 },
  )).data as Blob
}

export async function exportMobileTaskAnalysis(
  params: MobileTaskAnalysisSearchParams,
): Promise<Blob> {
  return (await api.post(
    '/mobile-tasks/analysis/export',
    {
      parser_types: params.parser_types,
      scope: params.scope,
      review_stage: params.review_stage || 'all',
      communities: params.communities || [],
      inspectors: params.inspectors || [],
      watch_categories: params.watch_categories || [],
      sort: params.sort || 'priority',
      keyword: params.keyword || '',
      page: 1,
      page_size: 50,
    },
    { ...activeRequest, responseType: 'blob', timeout: 300_000 },
  )).data as Blob
}

export async function importMobileTaskAnalysis(file: File): Promise<{
  success_count: number
  failed_count: number
  success: Array<{ row: number; task: string; state: string }>
  failed: Array<{ row: number; reason: string }>
}> {
  const form = new FormData()
  form.append('file', file)
  return (await api.post('/mobile-tasks/analysis/import', form, {
    ...activeRequest,
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 300_000,
  })).data
}

export async function selectMobileTasksForAssignment(
  params: MobileTaskSearchParams,
): Promise<{ row_keys: string[]; total: number; community: string }> {
  const { data } = await api.post(
    `/mobile-tasks/${encodeURIComponent(params.parser_type)}/assignment-selection`,
    mobileTaskSearchPayload(params),
    activeRequest,
  )
  return data
}

export interface MobileTaskAssignmentCandidate {
  row_key: string
  community: string
  source: string
  address: string
}

export async function getMobileTaskAssignmentWorkbench(
  parserType: string,
): Promise<{
  data: MobileTaskAssignmentCandidate[]
  total: number
  displayed_total: number
  limited: boolean
  limit: number
  communities: MobileTaskFilterOption[]
  inspectors_by_community: Record<string, string[]>
  inspector_counts_by_community: Record<string, Record<string, number>>
}> {
  const { data } = await api.get(
    `/mobile-tasks/${encodeURIComponent(parserType)}/assignment-workbench`,
    activeRequest,
  )
  return data
}

export async function getMobileTaskFilterOptions(
  parserType: string,
  scope: MobileTaskScope,
  communities: string[] = [],
  reviewStage: MobileTaskReviewStage = 'all',
  options: { passive?: boolean } = {},
): Promise<{
  source_ready: boolean
  communities: MobileTaskFilterOption[]
  inspectors: MobileTaskFilterOption[]
  assignment: {
    enabled: boolean
    community_aliases: Record<string, string>
    inspectors_by_community: Record<string, string[]>
  }
  watch_categories: Array<{
    value: number
    label: string
    color: string
    alert_level: string
    count: number
  }>
}> {
  const params = new URLSearchParams()
  params.set('scope', scope)
  params.set('review_stage', reviewStage)
  communities.forEach(value => params.append('community', value))
  const { data } = await api.get(
    `/mobile-tasks/${encodeURIComponent(parserType)}/filter-options`,
    { ...(options.passive ? {} : activeRequest), params },
  )
  return data
}

export async function getMobileTaskAnalysisFilterOptions(
  parserTypes: string[],
  communities: string[] = [],
  reviewStage: MobileTaskReviewStage = 'all',
  options: { passive?: boolean } = {},
): ReturnType<typeof getMobileTaskFilterOptions> {
  const params = new URLSearchParams()
  params.set('review_stage', reviewStage)
  parserTypes.forEach(value => params.append('parser_type', value))
  communities.forEach(value => params.append('community', value))
  return api.get('/mobile-tasks/analysis/filter-options', {
    ...(options.passive ? {} : activeRequest),
    params,
  }).then(response => response.data)
}

export async function getMobileTaskAnalysisDetail(
  parserType: string,
  rowKey: string,
): Promise<MobileTaskDetailData> {
  const { data } = await api.get(
    `/mobile-tasks/analysis/${encodeURIComponent(parserType)}/${encodeURIComponent(rowKey)}`,
    activeRequest,
  )
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

export async function getMobileTaskResidenceDetail(
  parserType: string,
  rowKey: string,
): Promise<ResidenceRegistrationDetail> {
  const { data } = await api.get(
    `/mobile-tasks/${encodeURIComponent(parserType)}/${encodeURIComponent(rowKey)}/residence-detail`,
    activeRequest,
  )
  return data
}

export async function previewQmfRegistration(payload: {
  parser_type: string
  row_key: string
  source_id: number
  expected_revision: number
}): Promise<QmfPreviewResult> {
  const { data } = await api.post('/qmf-registration/preview', payload, {
    ...activeRequest,
    timeout: 60000,
  })
  return data
}

export async function getQmfLegacyStatus(payload: {
  parser_type: string
  row_key: string
  source_id: number
  expected_revision: number
}): Promise<QmfLegacyStatus> {
  const { data } = await api.post('/qmf-registration/status', payload, {
    ...activeRequest,
    timeout: 60000,
  })
  return data
}

export async function getQmfConfig(): Promise<QmfConfig> {
  const { data } = await api.get('/qmf-registration/config', activeRequest)
  return data
}

export async function updateQmfConfig(payload: QmfConfigUpdate): Promise<QmfConfig> {
  const { data } = await api.put('/qmf-registration/config', payload, activeRequest)
  return data
}

export async function getResidencePlatformConfig(): Promise<ResidencePlatformConfig> {
  const { data } = await api.get('/residence-platform/config', activeRequest)
  return data
}

export async function updateResidencePlatformConfig(
  payload: ResidencePlatformConfigUpdate,
): Promise<ResidencePlatformConfig> {
  const { data } = await api.put('/residence-platform/config', payload, activeRequest)
  return data
}

export async function startResidencePlatformScan(): Promise<{
  run: ExternalAcquisitionRun
  reused: boolean
}> {
  const { data } = await api.post('/residence-platform/scan', {}, activeRequest)
  return data
}

export async function startQmfStatusScan(): Promise<QmfStatusScanRun> {
  const { data } = await api.post('/qmf-registration/status-scans', {}, activeRequest)
  return data.data
}

export async function getLatestQmfStatusScan(): Promise<QmfStatusScanRun | null> {
  const { data } = await api.get('/qmf-registration/status-scans/latest')
  return data.data || null
}

export async function getQmfStatusScan(runId: number): Promise<QmfStatusScanRun> {
  const { data } = await api.get(
    `/qmf-registration/status-scans/${runId}`,
  )
  return data.data
}

export async function getMobileTaskInlineEditors(
  parserType: string,
  rowKeys: string[],
  analysisMode = false,
): Promise<MobileTaskInlineEditorsData> {
  const prefix = analysisMode ? '/mobile-tasks/analysis' : '/mobile-tasks'
  const uniqueRowKeys = [...new Set(rowKeys.map(rowKey => String(rowKey).trim()).filter(Boolean))]
  const items: MobileTaskInlineEditorsData['items'] = {}
  for (let offset = 0; offset < uniqueRowKeys.length; offset += 50) {
    const { data } = await api.post<MobileTaskInlineEditorsData>(
      `${prefix}/${encodeURIComponent(parserType)}/inline-editors`,
      { row_keys: uniqueRowKeys.slice(offset, offset + 50) },
      activeRequest,
    )
    Object.assign(items, data.items)
  }
  return { items, analysis_mode: analysisMode }
}

export async function updateMobileTask(
  parserType: string,
  sourceId: number,
  payload: {
    changes: Record<string, string>
    base_values?: Record<string, string>
    expected_revision: number
    registration_property_id?: number
    registration_property_version?: number
  },
): Promise<{
  values: Record<string, string>
  row_key: string
  revision: number
  pending_sync: boolean
  sync_state: MobileTaskSyncState
  message: string
  warnings?: string[]
  inspector_mismatch?: boolean
}> {
  const { data } = await api.patch(
    `/mobile-tasks/${encodeURIComponent(parserType)}/source-rows/${sourceId}`,
    payload,
  )
  return data
}

export async function searchRegistrationProperties(
  keyword: string,
  communityName = '',
): Promise<{ data: MobileTaskRegistrationProperty[]; total: number }> {
  const { data } = await api.post('/registry/properties/search', {
    keyword,
    community_name: communityName,
    status: 'active',
    page: 1,
    page_size: 20,
  }, activeRequest)
  return data
}

export async function manuallyConfirmRegistration(
  parserType: string,
  rowKey: string,
  payload: {
    reason: 'address_mismatch' | 'address_ambiguous'
    note?: string
    expected_revision: number
  },
): Promise<{
  values: Record<string, string>
  row_key: string
  revision: number
  pending_sync: boolean
  sync_state: MobileTaskSyncState
  message: string
}> {
  const { data } = await api.post(
    `/mobile-tasks/${encodeURIComponent(parserType)}/${encodeURIComponent(rowKey)}/registration/confirm`,
    payload,
    activeRequest,
  )
  return data
}

export async function claimMobileTask(
  parserType: string,
  sourceId: number,
  payload: {
    changes: Record<string, string>
    base_values?: Record<string, string>
    expected_revision: number
    registration_property_id?: number
    registration_property_version?: number
  },
): Promise<{
  values: Record<string, string>
  row_key: string
  revision: number
  pending_sync: boolean
  sync_state: MobileTaskSyncState
  message: string
  warnings?: string[]
  inspector_mismatch?: boolean
}> {
  const { data } = await api.post(
    `/mobile-tasks/${encodeURIComponent(parserType)}/source-rows/${sourceId}/claim`,
    payload,
  )
  return data
}

export async function updateMobileTaskAnalysis(
  parserType: string,
  sourceId: number,
  payload: {
    changes: Record<string, string>
    base_values?: Record<string, string>
    expected_revision: number
  },
): Promise<{
  values: Record<string, string>
  row_key: string
  revision: number
  pending_sync: boolean
  sync_state: MobileTaskSyncState
  message: string
}> {
  const { data } = await api.patch(
    `/mobile-tasks/analysis/${encodeURIComponent(parserType)}/source-rows/${sourceId}`,
    payload,
  )
  return data
}

export async function decideMobileTaskUnverifiableReview(
  parserType: string,
  sourceId: number,
  payload: {
    stage: 'initial_pending' | 'deep_pending'
    outcome: 'success' | 'failure'
    opinion: string
    flow_version: number
    expected_revision: number
    expected_row_hash: string
  },
): Promise<{
  values: Record<string, string>
  row_key: string
  revision: number
  pending_sync: boolean
  sync_state: MobileTaskSyncState
  message: string
}> {
  const { data } = await api.patch(
    `/mobile-tasks/analysis/${encodeURIComponent(parserType)}/source-rows/${sourceId}/decision`,
    payload,
  )
  return data
}

export async function getTaskGraphConfig(): Promise<{ enabled: boolean; internal_only: boolean }> {
  const { data } = await api.get('/task-graph/config', activeRequest)
  return data
}

export async function updateTaskGraphConfig(enabled: boolean): Promise<{ enabled: boolean; internal_only: boolean }> {
  const { data } = await api.put('/task-graph/config', { enabled }, activeRequest)
  return data
}

export async function previewTaskGraphBackfill(): Promise<TaskGraphPreview> {
  const { data } = await api.post('/task-graph/backfill/preview', {}, activeRequest)
  return data
}

export async function runTaskGraphBackfill(): Promise<{ processed: number; changed: number }> {
  const { data } = await api.post('/task-graph/backfill', {}, activeRequest)
  return data
}

export async function getTaskGraphOptions(): Promise<{
  inspectors: MobileTaskFilterOption[]
  queues: Array<{ value: string; label: string }>
}> {
  const { data } = await api.get('/task-graph/options', activeRequest)
  return data
}

export async function searchTaskGraph(payload: {
  view: 'person' | 'queue'
  person_user_id?: number
  queue?: string
  history?: boolean
  task_types?: string[]
  keyword?: string
  cursors?: Record<string, number>
  page_size?: number
}, options: { passive?: boolean } = {}): Promise<TaskGraphSearchResponse> {
  const { data } = await api.post(
    '/task-graph/search',
    payload,
    options.passive ? passiveRequest : activeRequest,
  )
  return data
}

export async function bulkAssignMobileTasks(
  parserType: string,
  payload: {
    row_keys: string[]
    inspector?: string
    mode?: 'single' | 'balanced'
    balanced_offset?: number
    balanced_total?: number
  },
): Promise<{
  updated: number
  skipped: number
  failed: number
  details: Array<{ row_key: string; reason: string }>
  failed_details: Array<{ row_key: string; reason: string }>
  inspector: string
  mode: 'single' | 'balanced'
  assignment_counts: Record<string, number>
}> {
  const { data } = await api.post(
    `/mobile-tasks/${encodeURIComponent(parserType)}/bulk-assign`,
    payload,
    activeRequest,
  )
  return data
}

export const MOBILE_TASK_ASSIGNMENT_CHUNK_SIZE = 20

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
  qmf_community_code: string
  qmf_organization_codes: string[]
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
  qmfCommunityCode: string,
  qmfOrganizationCodes: string[] = [],
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
    qmf_community_code: qmfCommunityCode,
    qmf_organization_codes: qmfOrganizationCodes,
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

export type CodeSummarySource = 'peace' | 'manager'

export interface CodeSummaryRow {
  business_date: string
  raw_count: number
  total_people: number
  patrol_scan_count?: number
  dispatch_hall_scan_count?: number
  household_hall_scan_count?: number
  social_scan_count?: number
  unclassified_scan_count?: number
  active_accounts?: number
  instruction_count: number
  effective_warning_rate: number
  new_registration_count?: number
  effective_scan_rate?: number
  excluded_identity_count: number
  duplicate_removed_count: number
  version?: number
  run_id?: number
}

export interface CodeSummaryReport {
  source: CodeSummarySource
  start_date: string
  end_date: string
  columns: string[]
  data: CodeSummaryRow[]
  total: CodeSummaryRow
  latest_success_at: string | null
  latest_run: null | {
    id: number
    status: 'success' | 'warning' | 'failed'
    start_date: string
    end_date: string
    raw_count: number
    valid_count: number
    excluded_count: number
    duplicate_count: number
    unclassified_count: number
    invalid_time_count: number
    error_code: string | null
    error_message: string | null
    finished_at: string | null
    created_at: string | null
  }
}

export type CodeSummaryLocationClassification =
  | 'social' | 'patrol' | 'dispatch_hall' | 'household_hall' | 'ignored' | 'other' | 'unclassified'

export interface CodeSummaryLocationRow {
  location_key: string
  display_name: string
  record_count: number
  last_seen_date: string | null
  classification: CodeSummaryLocationClassification
}

export interface CodeSummaryLocationReport {
  source: CodeSummarySource
  start_date: string
  end_date: string
  data: CodeSummaryLocationRow[]
  total: number
  record_count: number
  unclassified_count: number
  classifications: Record<string, string>
}

export async function searchCodeSummaryLocations(payload: {
  source: CodeSummarySource
  start_date: string
  end_date: string
  keyword?: string
  status?: 'all' | 'unclassified' | 'classified'
  page?: number
  page_size?: number
}): Promise<CodeSummaryLocationReport> {
  return (await api.post('/code-summaries/locations/search', payload, activeRequest)).data
}

export async function saveCodeSummaryLocationClassifications(payload: {
  source: CodeSummarySource
  items: Array<{ location_key: string; display_name: string; classification: Exclude<CodeSummaryLocationClassification, 'unclassified'> }>
}): Promise<{ updated: number; message: string }> {
  return (await api.post('/code-summaries/locations/classifications', payload, activeRequest)).data
}

export async function recomputeCodeSummaryLocations(start_date: string, end_date: string): Promise<{ updated_days: number; message: string }> {
  return (await api.post('/code-summaries/locations/recompute', { start_date, end_date }, activeRequest)).data
}

export async function fetchCodeSummaries(startDate: string, endDate: string): Promise<{
  run: ExternalAcquisitionRun
  reused: boolean
}> {
  const { data } = await api.post('/code-summaries/fetch', {
    start_date: startDate,
    end_date: endDate,
  }, { ...activeRequest, timeout: 180_000 })
  return data
}

export interface ExternalAcquisitionRun {
  id: number
  kind: string
  status: 'queued' | 'running' | 'success' | 'warning' | 'failed' | 'interrupted'
  phase: string
  current: number
  total: number | null
  progress: number | null
  message: string
  result?: Record<string, any>
  error_code?: string | null
  error_message?: string | null
  created_at?: string | null
  started_at?: string | null
  finished_at?: string | null
}

export async function getExternalAcquisitionRun(
  runId: number,
  options: { passive?: boolean } = {},
): Promise<ExternalAcquisitionRun> {
  return (await api.get(
    `/external-acquisition/runs/${runId}`,
    options.passive ? passiveRequest : activeRequest,
  )).data
}

export async function getLatestExternalAcquisitionRun(
  kind: string,
  options: { passive?: boolean } = {},
): Promise<ExternalAcquisitionRun | null> {
  return (await api.get('/external-acquisition/latest', {
    ...(options.passive ? passiveRequest : activeRequest),
    params: { kind },
  })).data.data || null
}

export async function startQmfSourceSync(): Promise<{
  data: ExternalAcquisitionRun
  reused: boolean
}> {
  return (await api.post('/qmf-source/sync', {}, { ...activeRequest, timeout: 30_000 })).data
}

export interface SelfOwnedRosterResult {
  batch_id: number
  status: string
  rule_version: string
  workbook_count: number
  total_rows: number
  valid_rows: number
  invalid_rows: number
  duplicate_rows: number
  matched_tasks: number
  updated_tasks: number
  skipped_tasks: number
}

export async function importModelThreeSelfOwnedRoster(file: File): Promise<SelfOwnedRosterResult> {
  const formData = new FormData()
  formData.append('file', file)
  const { data } = await api.post('/qmf-source/self-owned/import', formData, {
    ...activeRequest,
    timeout: 300_000,
  })
  return data.data
}

export async function getLatestModelThreeSelfOwnedRoster(): Promise<SelfOwnedRosterResult | null> {
  const { data } = await api.get('/qmf-source/self-owned/latest', passiveRequest)
  return data.data || null
}

export async function getCodeSummary(
  source: CodeSummarySource,
  startDate: string,
  endDate: string,
): Promise<CodeSummaryReport> {
  const { data } = await api.post('/code-summaries/search', {
    source,
    start_date: startDate,
    end_date: endDate,
  }, activeRequest)
  return data
}

export async function getVisitSummary(
  startDate: string,
  endDate: string,
  category: VisitSummaryCategory = 'rental',
  filters?: { scope?: 'permission' | 'responsibility'; community?: string },
): Promise<VisitSummaryReport> {
  const { data } = await api.get('/visits/summary', {
    ...activeRequest,
    params: {
      start_date: startDate,
      end_date: endDate,
      category,
      ...filters,
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

export async function previewVisitSource(payload: {
  source: 'detail' | 'rating' | 'both'
  start_date: string
  end_date: string
}): Promise<{ run: ExternalAcquisitionRun; reused: boolean }> {
  const { data } = await api.post('/visits/sources/preview', payload, {
    ...activeRequest,
    timeout: 300000,
  })
  return data
}

export async function confirmVisitSource(payload: {
  run_ids: number[]
  strategy: 'replace' | 'keep'
}): Promise<{ data: Array<{ id: number; status: string; batch_id?: number }>; strategy: string }> {
  const { data } = await api.post('/visits/sources/confirm', payload, activeRequest)
  return data
}

export async function getVisitSourceStatus(): Promise<{
  business_date: string
  timezone: string
  data: Record<string, VisitSourceRun>
  latest_attempts: Record<string, VisitSourceRun>
  current_sources: Record<string, { batch_id: number; source_type: string; source_run_id: number | null; finished_at: string | null }>
  runs: VisitSourceRun[]
}> {
  const { data } = await api.get('/visits/sources/status', activeRequest)
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

// ---- 全链条预处理与小区管理 ----
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
  address_type: 'community' | 'apartment' | 'construction_dormitory' | 'other'
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
  publishable: number
  partial_publishable: number
}

export interface PoliceDispatchBatch {
  id: number
  file_name: string
  sheet_name: string
  import_mode: 'raw' | 'clean' | 'processed' | 'quick'
  business_type: 'fullchain' | 'rental' | 'police' | 'delivery' | 'suspect_return'
  police_subtype: 'internal' | 'suzhou' | 'traffic' | ''
  import_profile: string
  adapter_version: string
  target_parser: string
  business_date: string | null
  source_summary: Record<string, number>
  source_file_available: boolean
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

export interface PoliceImportProfile {
  key: string
  business_type: PoliceDispatchBatch['business_type']
  label: string
  police_subtype: PoliceDispatchBatch['police_subtype']
  target_parser: string
  enabled: boolean
  description: string
  example_fields: string[]
  adapter_version: string
  target_configured: boolean
}

export interface PoliceImportPreview {
  file_name: string
  sheet_name: string
  row_count: number
  profile: PoliceImportProfile
  business_date: string
  counts: {
    total: number
    importable: number
    missing_key: number
    duplicate: number
    identity_invalid: number
    community_invalid: number
    conflict: number
  }
  community_distribution: Array<{ community_id: number; community_name: string; count: number }>
  rows: Array<{
    source_row: number
    person_name: string
    identity_number: string
    phone: string
    community_name: string
    business_key: string
    result: 'importable' | 'problem'
    issues: Array<{ field: string; type: string; value: string }>
  }>
  rows_truncated: boolean
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
  allocation_mode: 'matched' | 'balanced' | 'conflict' | 'missing_phone' | ''
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
  standard_values: Record<string, string>
  validation_issues: Array<{ field: string; type: string; value: string }>
  business_key_hmac: string
  target_parser: string
  business_type: PoliceDispatchBatch['business_type']
  police_subtype: PoliceDispatchBatch['police_subtype']
  import_profile: string
}

export interface PoliceDispatchPublishRun {
  id: number
  batch_id: number
  status: 'pending' | 'running' | 'completed' | 'partial' | 'failed'
  phase: 'queued' | 'preparing' | 'reading_source' | 'publishing' | 'refreshing_cache' | 'finished'
  total_count: number
  processed_count: number
  success_count: number
  conflict_count: number
  reconciliation_count: number
  retryable_count: number
  error_code: string
  error_message: string
  started_at: string | null
  finished_at: string | null
  created_at: string | null
  updated_at: string | null
}

export async function listPoliceAddresses(params?: {
  keyword?: string
  enabled?: boolean
}): Promise<{
  data: PoliceAddressEntry[]
  total: number
  communities: PoliceCommunityOption[]
  community_locked: boolean
}> {
  const { data } = await api.get('/police-dispatch/addresses', { params })
  return data
}

export async function createPoliceAddress(payload: PoliceAddressPayload): Promise<void> {
  await api.post('/police-dispatch/addresses', payload)
}

export async function updatePoliceAddress(id: number, payload: PoliceAddressPayload): Promise<void> {
  await api.put(`/police-dispatch/addresses/${id}`, payload)
}

export async function deletePoliceAddress(id: number): Promise<void> {
  await api.delete(`/police-dispatch/addresses/${id}`)
}

export async function exportPoliceAddresses(payload: {
  keyword?: string
  enabled?: boolean
}): Promise<Blob> {
  const { data } = await api.post('/police-dispatch/addresses/export', payload, {
    responseType: 'blob',
    timeout: 120000,
  })
  return data
}

export async function uploadPoliceDispatchBatch(
  file: File,
  importMode: 'raw' | 'clean' = 'raw',
  options?: { confirm?: boolean; previewToken?: string },
): Promise<{
  status: 'success' | 'duplicate' | 'preview'
  message: string
  batch?: PoliceDispatchBatch
  preview_token?: string
  preview?: {
    file_name: string
    sheet_name: string
    row_count: number
    counts: Record<string, number>
    community_distribution: Array<{
      community_id: number
      community_name: string
      count: number
    }>
    rows: Array<{
      source_row: number
      person_name: string
      identity_number: string
      phone: string
      community_name: string
      registration_status: string
      result: 'dispatch' | 'manual'
      reason: string
    }>
    rows_truncated: boolean
  }
}> {
  const form = new FormData()
  form.append('file', file)
  form.append('import_mode', importMode)
  if (options?.confirm) form.append('confirm', 'true')
  if (options?.previewToken) form.append('preview_token', options.previewToken)
  const { data } = await api.post('/police-dispatch/batches', form, { timeout: 300000 })
  return data
}

export async function listPoliceDispatchBatches(params?: {
  file_name?: string
  upload_date?: string
  status?: string
  business_type?: string
  police_subtype?: string
  page?: number
  page_size?: number
}): Promise<{ data: PoliceDispatchBatch[]; total: number; page: number; page_size: number }> {
  const { data } = await api.get('/police-dispatch/batches', { params })
  return data
}

export async function getPoliceImportProfiles(): Promise<{ data: PoliceImportProfile[]; adapter_version: string }> {
  const { data } = await api.get('/police-dispatch/import-profiles')
  return data
}

export async function getQuickDispatchOptions(): Promise<{
  businesses: Array<{
    key: string
    label: string
    target_parser: string
    business_type: PoliceDispatchBatch['business_type']
    police_subtype: PoliceDispatchBatch['police_subtype']
    fields: Array<{ key: string; label: string; required: boolean; type: 'text' | 'textarea' | 'registration'; placeholder?: string }>
  }>
  communities: Array<{ id: number; name: string }>
}> {
  const { data } = await api.get('/police-dispatch/quick-dispatch/options')
  return data
}

export interface QuickDispatchPayload {
  request_id: string
  profile?: string
  fields?: Record<string, string>
  source_name?: string
  community_id?: number
  person_name?: string
  identity_number?: string
  phone?: string
  original_address?: string
  registration_status?: string
  business_date: string
  deadline_date?: string
  created_time?: string
}

export async function createQuickPoliceDispatch(payload: QuickDispatchPayload): Promise<{
  status: 'success' | 'duplicate'
  message: string
  batch: PoliceDispatchBatch
  task_id: number
}> {
  const { data } = await api.post('/police-dispatch/quick-dispatch', payload)
  return data
}

export async function previewPoliceDispatchImport(
  file: File,
  profile: string,
  businessDate: string,
): Promise<{ status: 'preview'; preview_token: string; file_sha256: string; preview: PoliceImportPreview }> {
  const form = new FormData()
  form.append('file', file)
  form.append('profile', profile)
  form.append('business_date', businessDate)
  const { data } = await api.post('/police-dispatch/imports/preview', form, { timeout: 300000 })
  return data
}

export async function confirmPoliceDispatchImport(
  file: File,
  profile: string,
  businessDate: string,
  previewToken: string,
): Promise<{ status: 'success' | 'duplicate'; message: string; batch: PoliceDispatchBatch }> {
  const form = new FormData()
  form.append('file', file)
  form.append('profile', profile)
  form.append('business_date', businessDate)
  form.append('preview_token', previewToken)
  const { data } = await api.post('/police-dispatch/imports/confirm', form, { timeout: 300000 })
  return data
}

export function policeDispatchSourceFileUrl(batchId: number): string {
  return `/api/police-dispatch/batches/${batchId}/source-file`
}

export async function getPoliceDispatchBatch(id: number): Promise<{
  batch: PoliceDispatchBatch
  communities: PoliceCommunityOption[]
}> {
  const { data } = await api.get(`/police-dispatch/batches/${id}`)
  return data
}

export async function deletePoliceDispatchBatch(id: number): Promise<{
  message: string
  deleted_task_count: number
}> {
  const { data } = await api.delete(`/police-dispatch/batches/${id}`)
  return data
}

export async function getPoliceDispatchWorkbench(
  options: { passive?: boolean } = {},
): Promise<{
  active_batch: PoliceDispatchBatch | null
  batches: PoliceDispatchBatch[]
  communities: PoliceCommunityOption[]
}> {
  const { data } = await api.get(
    '/police-dispatch/workbench/home',
    options.passive ? passiveRequest : activeRequest,
  )
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

export async function getPoliceDispatchPublishableSelection(params: {
  batch_id: number
  status?: string
  category?: string
  keyword?: string
}): Promise<{ task_ids: number[]; total: number }> {
  const { data } = await api.post('/police-dispatch/tasks/publishable-selection', {
    ...params,
    page: 1,
    page_size: 1,
  }, activeRequest)
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

export async function adoptExistingPoliceDispatchContent(
  id: number,
  payload: {
    expected_version: number
    expected_row_hash: string
  },
): Promise<{ message: string; cache_pending: boolean }> {
  const { data } = await api.post(`/police-dispatch/tasks/${id}/resolve-conflict`, {
    ...payload,
    // 兼容旧服务端枚举；正常页面只提供“采用本地现有内容”。
    strategy: 'adopt_tencent',
    confirmation: '',
  })
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

export async function resolvePoliceDispatchDuplicateGroup(
  keepTaskId: number,
  payload: {
    tasks: Array<{ id: number; version: number }>
    review_note?: string
  },
): Promise<{ message: string; keep_task_id: number; excluded_count: number }> {
  const { data } = await api.post(
    `/police-dispatch/tasks/${keepTaskId}/resolve-duplicate`,
    payload,
  )
  return data
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

export async function publishSelectedPoliceDispatchTasks(
  id: number,
  taskIds: number[],
): Promise<PoliceDispatchPublishRun & { message: string }> {
  const { data } = await api.post(`/police-dispatch/batches/${id}/publish-selected`, {
    task_ids: taskIds,
  })
  return data
}

export async function getLatestPoliceDispatchPublishRun(
  batchId: number,
): Promise<PoliceDispatchPublishRun | null> {
  const { data } = await api.get(
    `/police-dispatch/batches/${batchId}/publish-runs/latest`,
    activeRequest,
  )
  return data.data
}

export async function getPoliceDispatchPublishRun(
  runId: number,
): Promise<PoliceDispatchPublishRun> {
  const { data } = await api.get(`/police-dispatch/publish-runs/${runId}`, activeRequest)
  return data
}

export function policeDispatchFeedbackUrl(id: number): string {
  return `/api/police-dispatch/batches/${id}/feedback.xlsx`
}

export function apiErrorMessage(reason: unknown, fallback: string): string {
  const error = reason as {
    message?: unknown
    response?: { data?: { detail?: unknown } }
  }
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const messages = detail
      .map(item => (
        item && typeof item === 'object' && typeof (item as { msg?: unknown }).msg === 'string'
          ? (item as { msg: string }).msg
          : ''
      ))
      .filter(Boolean)
    if (messages.length) return messages.join('；')
  }
  if (detail && typeof detail === 'object') {
    const message = (detail as { message?: unknown }).message
    if (typeof message === 'string' && message.trim()) return message
  }
  return typeof error?.message === 'string' && error.message.trim()
    ? error.message
    : fallback
}

export interface FullchainArchiveCandidate {
  source_id: number
  row_key: string
  revision: number
  name: string
  identity: string
  phone: string
  address: string
  source: string
  result: string
  deadline: string
  stage: 'direct' | 'review' | 'registered'
  category: string
  eligible: boolean
  reason: string
  decision: string
  review_note: string
  source_count: number
  conflict: boolean
  registration_status: string
  registration_confirmed_at: string | null
  archive_available_at: string | null
  registration_property_id: number | null
  registration_property_version: number | null
  candidate_rule_version: string
}

export interface FullchainArchiveExport {
  id: number
  export_no: string
  parser_type?: string
  status: 'queued' | 'running' | 'completed' | 'partial' | 'failed'
  phase: string
  file_name: string
  total_count: number
  success_count: number
  conflict_count: number
  error_count: number
  categories: Record<string, number>
  error_message: string
  created_at: string
  started_at: string | null
  finished_at: string | null
  updated_at: string
  items: Array<{
    source_id: number
    category: string
    status: 'queued' | 'success' | 'conflict' | 'error'
    error_code: string
    error_stage: string
    external_delete_state: 'pending' | 'deleting' | 'deleted'
    external_deleted_at: string | null
    platform_archive_state: 'pending' | 'archiving' | 'archived' | 'failed' | 'reconciled'
    reconcile_state: 'pending' | 'reconciling' | 'reconciled_by_sync' | 'reconciled_from_current' | 'reconciled_from_snapshot' | 'conflict'
    reconcile_attempts: number
    error_fingerprint: string
    last_attempt_at: string | null
    reconciled_at: string | null
  }>
}

export async function listFullchainPoliceRawUploads() {
  const { data } = await api.get('/police-dispatch/fullchain-archive/police-raw/uploads')
  return data as { data: Array<{ id: number; file_name: string; row_count: number; invalid_count: number; duplicate_count: number; status: string; created_at: string }> }
}

export function fullchainPoliceRawDownloadUrl(id: number) {
  return `/api/police-dispatch/fullchain-archive/police-raw/uploads/${id}/download`
}

export async function searchFullchainArchiveCandidates(params: {
  parser_type?: string
  stages?: Array<'direct' | 'review' | 'registered'>
  keyword?: string
  page?: number
  page_size?: number
}) {
  const { data } = await api.post('/police-dispatch/fullchain-archive/candidates/search', params, activeRequest)
  return data as { data: FullchainArchiveCandidate[]; total: number; page: number; page_size: number; counts: Record<string, number> }
}

export async function selectFullchainArchiveCandidates(params: {
  parser_type?: string
  stages?: Array<'direct' | 'review' | 'registered'>
  keyword?: string
}) {
  const { data } = await api.post('/police-dispatch/fullchain-archive/candidates/selection', params, activeRequest)
  return data as { source_ids: number[]; total: number; max_total: number }
}

export async function saveFullchainArchiveReview(payload: {
  parser_type?: string
  row_key: string
  decision: 'transfer_internal' | 'transfer_external' | 'keep' | 'archive'
  note?: string
}) {
  const { data } = await api.post('/police-dispatch/fullchain-archive/reviews', payload)
  return data as { message: string }
}

export async function previewFullchainArchiveExport(sourceIds: number[], parserType = '全链条') {
  const { data } = await api.post(`/police-dispatch/fullchain-archive/exports/preview?parser_type=${encodeURIComponent(parserType)}`, sourceIds)
  return data as { total: number; categories: Record<string, number>; rows: Array<{ source_id: number; name: string; result: string; category: string; reason: string }>; preview_token: string }
}

export async function createFullchainArchiveExport(sourceIds: number[], previewToken: string, parserType = '全链条') {
  const { data } = await api.post(`/police-dispatch/fullchain-archive/exports?parser_type=${encodeURIComponent(parserType)}`, { source_ids: sourceIds, preview_token: previewToken })
  return data as { message: string; export: FullchainArchiveExport }
}

export async function listFullchainArchiveExports(parserType?: string) {
  const { data } = await api.get('/police-dispatch/fullchain-archive/exports', {
    ...passiveRequest,
    params: parserType ? { parser_type: parserType } : undefined,
  })
  return data as { data: FullchainArchiveExport[] }
}

export function fullchainArchiveDownloadUrl(id: number) {
  return `/api/police-dispatch/fullchain-archive/exports/${id}/download`
}

// ---- System Config ----
export async function getSystemConfig(): Promise<Record<string, string>> {
  const { data } = await api.get('/system/config')
  return data.data
}

export async function getMaintenanceStatus(): Promise<MaintenanceStatus> {
  const { data } = await api.get('/maintenance/status')
  return data as MaintenanceStatus
}

export async function updateSystemConfig(config: Record<string, string>): Promise<void> {
  await api.put('/system/config', config)
}

function utcDate(value: string): Date {
  const trimmed = value.trim()
  const databaseUtc = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/
  const normalized = databaseUtc.test(trimmed)
    ? `${trimmed.replace(' ', 'T')}Z`
    : trimmed
  return new Date(normalized)
}

// 数据库时间按 UTC 保存；无时区标记的 ISO 字符串也必须按 UTC 解释。
export function formatUTCTime(utcStr: string | null | undefined, timezone: string = 'Asia/Shanghai'): string {
  if (!utcStr) return '-'
  try {
    const date = utcDate(utcStr)
    if (Number.isNaN(date.getTime())) return utcStr
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: timezone,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hour12: false,
      hourCycle: 'h23',
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

// ---- Public work profiles ----
export async function listPublicProfiles(params?: {
  keyword?: string
  position?: string
  year?: number
  page?: number
  page_size?: number
}): Promise<{
  data: PublicProfileSummary[]
  total: number
  page: number
  page_size: number
  year: number
}> {
  const { data } = await api.get('/profiles', { params, ...activeRequest })
  return data
}

export async function getPublicProfile(
  userId: number,
  year?: number,
): Promise<PublicProfile> {
  const { data } = await api.get(`/profiles/${userId}`, {
    params: year ? { year } : undefined,
    ...activeRequest,
  })
  return data
}

// ---- Role dashboard ----
export interface DashboardMetricOverview {
  exists?: boolean
  total_tasks?: number
  carryover_tasks?: number
  new_tasks?: number
  changed_tasks?: number
  pending_tasks?: number
  completed_tasks?: number
  completion_rate?: number
  unable_to_verify?: number
  [key: string]: unknown
}

export interface RoleDashboardData {
  business_date: string
  last_success_at: string | null
  period: { start_date: string; end_date: string; days: number }
  identity: {
    user_id: number
    display_name: string
    position: string
    departments: string[]
  }
  scope: {
    kind: 'responsibility'
    label: string
    communities: string[] | null
  }
  sync: {
    id: number | null
    status: string
    trigger_source: string
    stage: string
    created_at: string | null
    finished_at: string | null
  }
  notifications: {
    unread_count: number
    personal_unread_count: number
    announcement_unread_count: number
  }
  contribution: {
    total: number
    active_days: number
    longest_streak: number
    days: Array<{ date: string; count: number }>
    categories: Array<{ type: string; label: string; count: number }>
    start_date: string
    end_date: string
    profile_user_id: number
  }
  flow_tasks: null | {
    available: boolean
    message?: string
    scope?: string
    community?: string
    personal?: {
      pending: number
      review: number
      new_today: number | null
      carryover_today: number | null
      completed_today: number | null
    }
    community_totals?: { pending: number; review: number }
    daily_snapshot_available?: boolean
    businesses: Array<{
      parser_type: string
      label: string
      pending: number
      unchecked: number
      checked: number
      completed: number
      review: number
    }>
    week_overview?: DashboardMetricOverview
  }
  online_overview: null | {
    scope: string
    scope_label: string
    communities: string[] | null
    today: DashboardMetricOverview
    week: DashboardMetricOverview
    community_breakdown: Array<{
      community: string
      total: number
      pending: number
      completed: number
      unable_to_verify: number
      completion_rate: number
    }>
  }
  visit_overview: null | {
    category: 'rental' | 'self_owned'
    scope: string
    scope_label?: string
    today: Record<string, any>
    week: Record<string, any>
    attendance?: Record<string, any>
    community_breakdown: Array<Record<string, any>>
  }
  dispatch_overview: null | {
    active_batch: PoliceDispatchBatch | null
  }
  management: null | {
    sync: RoleDashboardData['sync']
    online_writeback_enabled: boolean
    dispatch_exceptions: number
    latest_backup?: {
      status: string
      created_at: string | null
      finished_at: string | null
    }
  }
}

export async function getRoleDashboard(): Promise<RoleDashboardData> {
  const { data } = await api.get('/dashboard', activeRequest)
  return data
}

export interface RegistryProperty {
  id: number
  community_id: number | null
  community_name: string
  natural_address: string
  building: string
  room: string
  housing_type: string
  residence_type: string
  source_house_no: string
  source_updated_at: string | null
  source_type: string
  source_ref: string
  normalized_address: string
  status: string
  version: number
  updated_at: string | null
  certificate_status: RegistryCertificateStatus
  certificate_status_label: string
  certificate_count: number
  certificate_issue_count: number
  certificate_source_ready: boolean
  certificate_updated_at: string | null
  landlord_renter_relation: 'same' | 'different' | 'unknown' | 'not_required' | 'conflict'
  landlord_renter_relation_label: string
  actual_renter_status: 'confirmed' | 'unknown'
  responsibility_identity: string
  visit_count: number
  latest_visit_date: string | null
  latest_star_rating: string | null
  latest_star_rating_at: string | null
}

export interface RegistryPropertyVisit {
  id: number
  community: string
  entry_method: string
  address: string
  operator_name: string
  visited_at: string | null
  business_date: string
  room_check_count: number
  added_count: number
  changed_count: number
  cancelled_count: number
  star_rating: string | null
  score: number | null
  star_rated_at: string | null
  star_rating_date: string | null
}

export interface RegistryPerson {
  id: number
  name: string
  identity_number: string
  has_identity: boolean
  is_temporary: boolean
  verification_status: string
  status: string
  updated_at: string | null
  categories?: Array<{
    assignment_id: number
    id: number
    code: string
    name: string
    color: string
    alert_level: string
  }>
}

export interface RegistryOrganization {
  id: number
  name: string
  organization_type: string
  license_number: string
  status: string
  notes: string
  updated_at: string | null
}

export interface RegistryImportIssue {
  id: number
  batch_id: number | null
  issue_type: string
  source_type: string
  source_ref: string
  entity_key: string
  payload: Record<string, unknown>
  reason: string
  status: string
  review_note: string
  reviewed_by: number | null
  reviewed_at: string | null
  created_at: string | null
  problem_details: Array<{ field: string; value: string }>
}

export type RegistryHousingCategory = '' | 'rental' | 'self_owned' | 'other' | 'unmarked'
export type RegistryCertificateStatus = '' | 'normal_signed' | 'not_required' | 'not_uploaded'
  | 'renter_needs_correction' | 'actual_renter_missing' | 'multiple_or_conflict'
  | 'not_applicable'

export interface RegistryCertificateSourceRun {
  id: number
  status: 'pending' | 'running' | 'completed' | 'failed'
  phase: 'queued' | 'reading' | 'classifying' | 'writing_preview' | 'finished'
  current_page: number
  fetched_count: number
  accepted_count: number
  rejected_count: number
  batch_id: number | null
  preview: {
    batch_id?: number
    status?: string
    idempotent?: boolean
    total_count?: number
    normal_count?: number
    issue_count?: number
    problem_row_count?: number
    duplicate_groups?: number
    conflict_groups?: number
  }
  error_code: string | null
  error_message: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string | null
  updated_at: string | null
  trigger_source: 'manual' | 'scheduled'
  business_date: string | null
  reused?: boolean
}

export interface WatchCategory {
  id: number
  code: string
  name: string
  parent_id: number | null
  color: string
  alert_level: string
  is_active: boolean
  description?: string
}

export interface WatchPerson {
  id: number
  name: string
  identity_number: string
  has_identity: boolean
  verification_status: string
  status: string
  created_at: string | null
  registry_person_id?: number | null
  is_registry_linked?: boolean
  categories: Array<Pick<WatchCategory, 'id' | 'code' | 'name' | 'color' | 'alert_level'>>
}

export interface WatchImportPreview {
  batch_id: number
  status: 'preview' | 'imported'
  idempotent: boolean
  file_count: number
  category_code: string
  total_rows: number
  valid_rows: number
  unique_people: number
  duplicate_rows: number
  missing_identity_count: number
  invalid_identity_count: number
  missing_name_count: number
  name_conflict_groups: number
  phone_conflict_groups: number
  existing_name_conflict_count: number
  inactive_people_count: number
  blocking_count: number
  existing_people: number
  new_people: number
  existing_assignments: number
  new_assignments: number
  can_confirm: boolean
}

export interface WatchImportResult {
  batch_id: number
  status: 'imported'
  idempotent: boolean
  unique_people?: number
  created_people?: number
  reused_people?: number
  created_assignments?: number
  existing_assignments?: number
  created_phones?: number
}

export const registryApi = {
  async properties(params: {
    keyword?: string
    community_id?: number
    housing_category?: RegistryHousingCategory
    certificate_status?: RegistryCertificateStatus
    status?: '' | 'active' | 'inactive'
    visit_start_date?: string
    visit_end_date?: string
    star_ratings?: string[]
    sort?: 'id_desc' | 'address_asc' | 'community_asc' | 'updated_desc' | 'visit_desc'
    page?: number
    page_size?: number
  } = {}) {
    return (await api.post('/registry/properties/search', params, activeRequest)).data as {
      data: RegistryProperty[]; total: number; page: number; page_size: number
    }
  },
  async exportProperties(params: {
    keyword?: string
    community_id?: number
    housing_category?: RegistryHousingCategory
    certificate_status?: RegistryCertificateStatus
    status?: '' | 'active' | 'inactive'
    visit_start_date?: string
    visit_end_date?: string
    star_ratings?: string[]
    sort?: 'id_desc' | 'address_asc' | 'community_asc' | 'updated_desc' | 'visit_desc'
  } = {}) {
    return (await api.post('/registry/properties/export', params, {
      ...activeRequest,
      responseType: 'blob',
      timeout: 300_000,
    })).data as Blob
  },
  async property(id: number) {
    return (await api.get(`/registry/properties/${id}`, activeRequest)).data
  },
  async propertyVisits(id: number, params: { page?: number; page_size?: number } = {}) {
    return (await api.get(`/registry/properties/${id}/visits`, {
      ...activeRequest,
      params,
    })).data as {
      data: RegistryPropertyVisit[]; total: number; page: number; page_size: number
    }
  },
  async certificateImage(propertyId: number, certificateId: number) {
    return (await api.get(
      `/registry/properties/${propertyId}/certificates/${certificateId}/image`,
      { ...activeRequest, responseType: 'blob' },
    )).data as Blob
  },
  async createProperty(payload: Record<string, unknown>) {
    return (await api.post('/registry/properties', payload)).data
  },
  async updateProperty(id: number, payload: Record<string, unknown>) {
    return (await api.put(`/registry/properties/${id}`, payload)).data
  },
  async previewHouseholdImport(file: File) {
    const form = new FormData()
    form.append('file', file)
    return (await api.post('/registry/imports/households/preview', form, {
      ...activeRequest,
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 300_000,
    })).data as {
      batch_id: number; status: string; idempotent: boolean; total_count: number; normal_count: number
      issue_count: number; duplicate_groups: number; other_type_count: number
      issue_breakdown?: Record<string, number>
    }
  },
  async confirmHouseholdImport(batchId: number) {
    return (await api.post(`/registry/imports/households/${batchId}/confirm`, {}, {
      ...activeRequest,
      timeout: 300_000,
    })).data as {
      batch_id: number; status: string; imported_count: number; idempotent: boolean
    }
  },
  async previewCertificateSource() {
    return (await api.post('/registry/imports/certificates/source-preview', {}, {
      ...activeRequest,
      timeout: 300_000,
    })).data as {
      batch_id: number; status: string; idempotent: boolean; total_count: number; normal_count: number
      issue_count: number; problem_row_count: number; duplicate_groups: number; conflict_groups: number
      source_record_count: number; source_rejected_count: number
    }
  },
  async startCertificateSourceRun() {
    return (await api.post('/registry/imports/certificates/source-runs', {}, activeRequest)).data as RegistryCertificateSourceRun
  },
  async latestCertificateSourceRun() {
    return (await api.get('/registry/imports/certificates/source-runs/latest')).data as {
      data: RegistryCertificateSourceRun | null
    }
  },
  async certificateSourceRun(runId: number) {
    return (await api.get(`/registry/imports/certificates/source-runs/${runId}`)).data as RegistryCertificateSourceRun
  },
  async retryCertificateSourceRun(runId: number, restart = false) {
    return (await api.post(`/registry/imports/certificates/source-runs/${runId}/retry`, { restart }, activeRequest)).data as RegistryCertificateSourceRun
  },
  async confirmCertificateImport(batchId: number) {
    return (await api.post(`/registry/imports/certificates/${batchId}/confirm`, {}, {
      ...activeRequest,
      timeout: 300_000,
    })).data as {
      batch_id: number; status: string; imported_count: number; skipped_count: number
      pending_issue_count: number; idempotent: boolean
    }
  },
  async importIssues(params: {
    keyword?: string
    status?: '' | 'pending' | 'resolved' | 'dismissed'
    issue_type?: string
    source_type?: '' | 'household' | 'certificate'
    community_id?: number
    housing_category?: RegistryHousingCategory
    page?: number
    page_size?: number
  } = {}) {
    return (await api.post('/registry/import/issues/search', params, activeRequest)).data as {
      data: RegistryImportIssue[]; total: number; page: number; page_size: number
    }
  },
  async reviewImportIssue(id: number, payload: { action: 'accept' | 'reject'; reason: string }) {
    return (await api.post(`/registry/import/issues/${id}/review`, payload, activeRequest)).data
  },
  async changePropertyStatus(id: number, payload: { status: 'active' | 'inactive'; reason?: string }) {
    return (await api.post(`/registry/properties/${id}/status`, payload)).data
  },
  async addPropertyAlias(id: number, payload: Record<string, unknown>) {
    return (await api.post(`/registry/properties/${id}/aliases`, payload)).data
  },
  async changeAliasStatus(id: number, payload: { status: 'active' | 'inactive'; reason?: string }) {
    return (await api.put(`/registry/aliases/${id}/status`, payload)).data
  },
  async addPropertyPersonRelation(id: number, payload: Record<string, unknown>) {
    return (await api.post(`/registry/properties/${id}/people`, payload)).data
  },
  async updatePropertyPersonRelation(id: number, payload: Record<string, unknown>) {
    return (await api.put(`/registry/property-person-relations/${id}`, payload)).data
  },
  async addPropertyOrganizationRelation(id: number, payload: Record<string, unknown>) {
    return (await api.post(`/registry/properties/${id}/organizations`, payload)).data
  },
  async updatePropertyOrganizationRelation(id: number, payload: Record<string, unknown>) {
    return (await api.put(`/registry/property-organization-relations/${id}`, payload)).data
  },
  async updateOrganization(id: number, payload: Record<string, unknown>) {
    return (await api.put(`/registry/organizations/${id}`, payload)).data
  },
  async attachOrganizationMember(id: number, payload: Record<string, unknown>) {
    return (await api.post(`/registry/organizations/${id}/members`, payload)).data
  },
  async updateOrganizationMembership(id: number, payload: Record<string, unknown>) {
    return (await api.put(`/registry/organization-memberships/${id}`, payload)).data
  },
  async mergePerson(id: number, payload: Record<string, unknown>) {
    return (await api.post(`/registry/people/${id}/merge`, payload)).data
  },
  async undoMerge(id: number) {
    return (await api.post(`/registry/merges/${id}/undo`, {})).data
  },
  async mergeHistory(params: { page?: number; page_size?: number } = {}) {
    return (await api.get('/registry/merges', { ...activeRequest, params })).data
  },
  async people(params: { page?: number; page_size?: number; category_ids?: number[] } = {}) {
    return (await api.get('/registry/people', { ...activeRequest, params })).data as {
      data: RegistryPerson[]; total: number; page: number; page_size: number
    }
  },
  async searchPeople(payload: Record<string, unknown>) {
    return (await api.post('/registry/people/search', payload, activeRequest)).data as {
      data: RegistryPerson[]; total: number; page: number; page_size: number
    }
  },
  async exportPeople(payload: {
    name?: string
    identity_number?: string
    phone?: string
    category_ids?: number[]
  } = {}) {
    return (await api.post('/registry/people/export', payload, {
      ...activeRequest,
      responseType: 'blob',
      timeout: 300_000,
    })).data as Blob
  },
  async roleTypes() {
    return (await api.get('/registry/role-types', activeRequest)).data as { data: Array<{ id: number; code: string; name: string; subject_type: 'person' | 'organization'; is_active: boolean }> }
  },
  async person(id: number) {
    return (await api.get(`/registry/people/${id}`, activeRequest)).data
  },
  async createPerson(payload: Record<string, unknown>) {
    return (await api.post('/registry/people', payload)).data
  },
  async updatePerson(id: number, payload: Record<string, unknown>) {
    return (await api.put(`/registry/people/${id}`, payload)).data
  },
  async addPhone(id: number, payload: Record<string, unknown>) {
    return (await api.post(`/registry/people/${id}/phones`, payload)).data
  },
  async addPersonTag(id: number, payload: Record<string, unknown>) {
    return (await api.post(`/registry/people/${id}/tags`, payload, activeRequest)).data
  },
  async releasePersonTag(id: number, assignmentId: number) {
    return (await api.post(`/registry/people/${id}/tags/${assignmentId}/release`, {}, activeRequest)).data
  },
  async organizations(params: { keyword?: string; page?: number; page_size?: number } = {}) {
    return (await api.get('/registry/organizations', { ...activeRequest, params })).data as {
      data: RegistryOrganization[]; total: number; page: number; page_size: number
    }
  },
  async exportOrganizations(params: { keyword?: string } = {}) {
    return (await api.post('/registry/organizations/export', params, {
      ...activeRequest,
      responseType: 'blob',
      timeout: 300_000,
    })).data as Blob
  },
  async organization(id: number) {
    return (await api.get(`/registry/organizations/${id}`, activeRequest)).data
  },
  async createOrganization(payload: Record<string, unknown>) {
    return (await api.post('/registry/organizations', payload)).data
  },
  async candidates(status = 'pending') {
    return (await api.get('/registry/change-candidates', { ...activeRequest, params: { status } })).data
  },
  async reviewCandidate(id: number, payload: { action: 'accept' | 'reject'; reason: string }) {
    return (await api.post(`/registry/change-candidates/${id}/review`, payload)).data
  },
  async conflicts(status = 'pending') {
    return (await api.get('/registry/conflicts', { ...activeRequest, params: { status } })).data
  },
  async reviewConflict(id: number, payload: { action: 'accept' | 'reject'; reason: string }) {
    return (await api.post(`/registry/conflicts/${id}/review`, payload)).data
  },
  async watchCategories() {
    return (await api.get('/registry/watch/categories', activeRequest)).data as { data: WatchCategory[] }
  },
  async createWatchCategory(payload: Record<string, unknown>) {
    return (await api.post('/registry/watch/categories', payload)).data
  },
  async updateWatchCategory(id: number, payload: Record<string, unknown>) {
    return (await api.put(`/registry/watch/categories/${id}`, payload)).data
  },
  async watchPeople(params: { page?: number; page_size?: number } = {}) {
    return (await api.get('/registry/watch/people', { ...activeRequest, params })).data as {
      data: WatchPerson[]; total: number; page: number; page_size: number
    }
  },
  async searchWatchPeople(payload: {
    keyword?: string
    category_ids?: number[]
    page?: number
    page_size?: number
  } = {}) {
    return (await api.post('/registry/watch/people/search', payload, activeRequest)).data as {
      data: WatchPerson[]; total: number; page: number; page_size: number
    }
  },
  async previewWatchImport(files: File[], categoryCode: string) {
    const form = new FormData()
    files.forEach(file => form.append('files', file))
    form.append('category_code', categoryCode)
    return (await api.post('/registry/watch/imports/preview', form, {
      ...activeRequest,
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 300_000,
    })).data as WatchImportPreview
  },
  async confirmWatchImport(batchId: number) {
    return (await api.post(`/registry/watch/imports/${batchId}/confirm`, {}, {
      ...activeRequest,
      timeout: 300_000,
    })).data as WatchImportResult
  },
  async watchPerson(id: number) {
    return (await api.get(`/registry/watch/people/${id}`, activeRequest)).data
  },
  async createWatchPerson(payload: Record<string, unknown>) {
    return (await api.post('/registry/watch/people', payload)).data
  },
  async updateWatchPerson(id: number, payload: Record<string, unknown>) {
    return (await api.put(`/registry/watch/people/${id}`, payload)).data
  },
  async createWatchAssignment(payload: Record<string, unknown>) {
    return (await api.post('/registry/watch/assignments', payload)).data
  },
  async updateWatchAssignment(id: number, payload: Record<string, unknown>) {
    return (await api.put(`/registry/watch/assignments/${id}`, payload)).data
  },
}

export interface WorkflowType {
  id: number
  code: string
  name: string
  description: string
  form_schema: { fields?: Array<Record<string, unknown>> }
  default_due_hours: number | null
  enabled: boolean
}

export interface WorkOrderSummary {
  id: number
  ticket_no: string
  type_code: string
  title: string
  requester_user_id: number | null
  current_assignee_user_id: number | null
  current_queue: string
  status: string
  priority: string
  due_at: string | null
  version_no: number
  updated_at: string
  overdue: boolean
}

export interface WorkOrderStep {
  id: number
  step_order: number
  name: string
  step_type: 'approval' | 'handling'
  status: string
  assignee_user_id: number | null
  queue: string
  due_at: string | null
  decision: string
  decision_note: string
  decided_by: number | null
  decided_at: string | null
  version_no: number
}

export interface WorkOrderAttachment {
  file_id: string
  original_name: string
  mime_type: string
  size_bytes: number
  sha256: string
  retention_until: string | null
  deleted_at: string | null
  created_at: string
}

export interface WorkOrderDetail extends WorkOrderSummary {
  description: string
  requester_user_id: number | null
  form_data: Record<string, unknown>
  type_detail?: Record<string, unknown>
  steps: WorkOrderStep[]
  links: Array<{ object_type: string; object_id: string; object_ref: string }>
  events: Array<Record<string, any>>
  comments: Array<{ user_id: number; content: string; created_at: string }>
  attachments: WorkOrderAttachment[]
}

export interface PhotoImportItem {
  safe_name: string
  person_name: string
  identity_number: string
  size_bytes: number
  sha256: string
  match_status: 'matched' | 'unmatched' | 'duplicate' | 'conflict' | 'failed'
  match_reason: string
  matched_ticket_ids: number[]
}

export interface PhotoImportBatch {
  id: number
  batch_no: string
  status: 'preview' | 'processing' | 'completed' | 'partial' | 'failed'
  total_files: number
  matched_files: number
  unmatched_files: number
  conflict_files: number
  duplicate_files: number
  failed_files: number
  error_message: string
  previewed_at: string | null
  confirmed_at: string | null
  created_at: string | null
  updated_at: string | null
  items?: PhotoImportItem[]
}

export interface PhotoImportReconcileResult {
  batch_id: number
  batch_no: string
  eligible_tickets: number
  attachment_copies: number
  already_attached: number
  manual_review_tickets: number
  missing_source_files: number
  repaired_tickets: number
}

export interface PendingPhotoRequest {
  id: number
  ticket_no: string
  title: string
  requester_user_id: number | null
  current_queue: string
  status: string
  priority: string
  due_at: string | null
  version_no: number
  updated_at: string | null
  subject_name: string
  identity_number: string
  community_name: string
  source_label: string
  requester_name: string
  requested_at: string | null
  overdue: boolean
}

export const workflowApi = {
  async types() {
    return (await api.get('/workflow/types', activeRequest)).data as { data: WorkflowType[] }
  },
  async createType(payload: Record<string, unknown>) {
    return (await api.post('/workflow/types', payload)).data
  },
  async versions(typeId: number) {
    return (await api.get(`/workflow/types/${typeId}/versions`, activeRequest)).data
  },
  async version(versionId: number) {
    return (await api.get(`/workflow/versions/${versionId}`, activeRequest)).data
  },
  async createVersion(typeId: number, payload: Record<string, unknown>) {
    return (await api.post(`/workflow/types/${typeId}/versions`, payload)).data
  },
  async updateVersion(versionId: number, payload: Record<string, unknown>) {
    return (await api.put(`/workflow/versions/${versionId}`, payload)).data
  },
  async publishVersion(versionId: number) {
    return (await api.post(`/workflow/versions/${versionId}/publish`)).data
  },
  async search(payload: Record<string, unknown>) {
    return (await api.post('/workflow/tickets/search', payload, activeRequest)).data as {
      data: WorkOrderSummary[]; total: number; page: number; page_size: number
    }
  },
  async ticket(id: number) {
    return (await api.get(`/workflow/tickets/${id}`, activeRequest)).data as WorkOrderDetail
  },
  async createTicket(payload: Record<string, unknown>) {
    return (await api.post('/workflow/tickets', payload)).data
  },
  async claim(id: number, expectedVersion: number) {
    return (await api.post(`/workflow/tickets/${id}/claim`, { expected_version: expectedVersion })).data
  },
  async decide(id: number, payload: Record<string, unknown>) {
    return (await api.post(`/workflow/tickets/${id}/decision`, payload)).data
  },
  async supplement(id: number, payload: Record<string, unknown>) {
    return (await api.post(`/workflow/tickets/${id}/supplement`, payload)).data
  },
  async restoreQueued(id: number, payload: { expected_version: number; reason: string }) {
    return (await api.post(`/workflow/tickets/${id}/restore-queued`, payload)).data
  },
  async withdraw(id: number, payload: Record<string, unknown>) {
    return (await api.post(`/workflow/tickets/${id}/withdraw`, payload)).data
  },
  async transfer(id: number, payload: Record<string, unknown>) {
    return (await api.post(`/workflow/tickets/${id}/transfer`, payload)).data
  },
  async comments(id: number, content: string, expectedVersion: number) {
    return (await api.post(`/workflow/tickets/${id}/comments`, {
      content,
      expected_version: expectedVersion,
    })).data
  },
  async attachments(id: number) {
    return (await api.get(`/workflow/tickets/${id}/attachments`, activeRequest)).data
  },
  async uploadAttachment(id: number, file: File, expectedVersion: number) {
    const form = new FormData()
    form.append('file', file)
    form.append('expected_version', String(expectedVersion))
    return (await api.post(`/workflow/tickets/${id}/attachments`, form)).data
  },
  async deleteAttachment(id: number, fileId: string, expectedVersion: number) {
    return (await api.delete(`/workflow/tickets/${id}/attachments/${encodeURIComponent(fileId)}`, {
      params: { expected_version: expectedVersion },
    })).data
  },
  attachmentUrl(id: number, fileId: string, inline = false) {
    return `/api/workflow/tickets/${id}/attachments/${encodeURIComponent(fileId)}${inline ? '?inline=true' : ''}`
  },
  async pendingPhotoRequests(payload: {
    keyword?: string
    community?: string
    source_label?: string
    page?: number
    page_size?: number
  } = {}, options: { passive?: boolean } = {}) {
    return (await api.post(
      '/workflow/photo-requests/pending/search',
      payload,
      options.passive ? passiveRequest : activeRequest,
    )).data as {
      data: PendingPhotoRequest[]; total: number; page: number; page_size: number
    }
  },
  async batchClaimPhotoRequests(payload: { ticket_ids?: number[]; claim_all?: boolean }) {
    return (await api.post('/workflow/photo-requests/batch-claim', payload)).data as {
      claimed_ids: number[]; skipped_ids: number[]; claimed_count: number
    }
  },
  async exportPendingPhotoRequests(payload: {
    keyword?: string
    community?: string
    source_label?: string
  } = {}) {
    return (await api.post('/workflow/photo-requests/pending/export', payload, {
      ...activeRequest,
      responseType: 'blob',
    })).data as Blob
  },
  async previewPhotoImport(file: File) {
    const form = new FormData()
    form.append('file', file)
    return (await api.post('/workflow/photo-imports/preview', form, {
      ...activeRequest,
      timeout: 300000,
    })).data as PhotoImportBatch
  },
  async confirmPhotoImport(batchId: number) {
    return (await api.post(`/workflow/photo-imports/${batchId}/confirm`, {}, {
      ...activeRequest,
      timeout: 300000,
    })).data as PhotoImportBatch
  },
  async photoImports(page = 1, pageSize = 20) {
    return (await api.get('/workflow/photo-imports', {
      ...activeRequest,
      params: { page, page_size: pageSize },
    })).data as { data: PhotoImportBatch[]; total: number; page: number; page_size: number }
  },
  async photoImport(batchId: number) {
    return (await api.get(`/workflow/photo-imports/${batchId}`, activeRequest)).data as PhotoImportBatch
  },
  async previewPhotoImportReconcile(batchId: number) {
    return (await api.get(`/workflow/photo-imports/${batchId}/reconcile-preview`, activeRequest)).data as PhotoImportReconcileResult
  },
  async reconcilePhotoImport(batchId: number) {
    return (await api.post(`/workflow/photo-imports/${batchId}/reconcile`, { confirm: true }, activeRequest)).data as PhotoImportReconcileResult
  },
}
