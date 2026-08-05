// 在线表格
export interface Spreadsheet {
  id: number
  name: string
  url: string
  file_id: string
  data_sheet_id: string
  summary_sheet_id: string
  header_row: number
  parser_type: string
  enabled: boolean
  created_at: string | null
  updated_at: string | null
}

export interface SpreadsheetCreate {
  name: string
  url: string
  parser_type?: string
  file_id?: string
  data_sheet_id?: string
  summary_sheet_id?: string
  header_row?: number
  enabled?: boolean
}

// 统计数据
export interface StatsItem {
  核查人: string
  下发日期: string
  数据总数: number
  已核查: number
  未核查: number
  核查完成率: number
  无法核实: number
  移交: number
  已登记: number
  通勤: number
  离苏: number
  空白: number
  无法见底数: number
  核查见底率: number
  computed_at: string | null
}

export interface StatsResponse {
  data: StatsItem[]
  total: number
  page: number
  page_size: number
}

// 同步状态
export type SyncStatusValue =
  | 'no_data'
  | 'pending'
  | 'running'
  | 'success'
  | 'completed'
  | 'partial'
  | 'failed'
  | 'conflict'

export interface SyncSchedule {
  enabled: boolean
  interval_minutes: number
  next_run_at: string | null
  server_time: string | null
}

export interface SyncStatus {
  task_id: number
  status: SyncStatusValue
  total_rows: number
  processed_rows: number
  error_message: string | null
  started_at: string | null
  finished_at: string | null
  trigger_source: 'manual' | 'scheduled'
  phase: 'queued' | 'syncing' | 'building_reports' | 'finished'
  current_item: string | null
  total_steps: number
  completed_steps: number
  last_success_at: string | null
  schedule: SyncSchedule
}

export interface SyncTriggerResponse {
  task_id: number
  status: 'pending' | 'conflict'
  message: string
}

export interface AppNotification {
  id: number
  source: 'announcement' | 'personal'
  category: string
  severity: 'error' | 'warning' | 'info'
  title: string
  content: string
  related_task_id: number | null
  is_read: boolean
  created_at: string
  read_at: string | null
}

// 超级管理员运维中心
export interface OpsContainer {
  source: 'backend' | 'mysql'
  name: string
  image?: string
  status: string
  health?: string | null
  started_at?: string | null
  restart_count?: number
  cpu_percent?: number
  memory_used_bytes?: number
  memory_limit_bytes?: number
  network_rx_bytes?: number
  network_tx_bytes?: number
  error?: string
}

export interface OpsDatabase {
  name: 'OnlineData' | 'OnlineDataArchive' | 'daily_report'
  purpose: string
  table_count: number
  estimated_rows: number
  data_bytes: number
  index_bytes: number
  engine_update_at: string | null
  last_activity_at: string | null
}

export interface BackupSchedule {
  enabled: boolean
  run_hour: number
  run_minute: number
  retention_days: number
  next_run_at: string | null
  last_triggered_at: string | null
  server_time: string | null
}

export interface BackupJob {
  id: number
  trigger_source: 'manual' | 'scheduled'
  status: 'pending' | 'running' | 'success' | 'failed' | 'expired'
  filename: string | null
  size_bytes: number | null
  sha256: string | null
  error_message: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string | null
  requested_by: string | null
}

export interface OpsOverview {
  server_time: string | null
  containers: OpsContainer[]
  container_error: string | null
  disk: {
    total_bytes: number
    free_bytes: number
    used_bytes: number
    free_percent: number
  }
  mysql: {
    connected: boolean
    version?: string
    connections?: number
    max_connections?: number
    error?: string
  }
  databases: OpsDatabase[]
  latest_sync: {
    id: number
    status: string
    trigger_source: string
    finished_at: string | null
  } | null
  latest_backup: {
    id: number
    status: string
    finished_at: string | null
    size_bytes: number | null
  } | null
  backup_schedule: BackupSchedule
  oauth: {
    configured: boolean
    status: 'not_configured' | 'unknown' | 'expired' | 'expiring' | 'healthy'
    expires_at?: string | null
  }
}

export interface AuditEvent {
  id: number
  user_id: number | null
  username: string
  action: string
  target_type: string
  target_name: string
  result: string
  detail: Record<string, unknown> | null
  ip_address: string
  user_agent: string
  created_at: string
}

// OAuth
export interface OAuthConfig {
  client_id: string
  client_secret: string
  access_token: string
  refresh_token?: string
  open_id: string
  expires_at?: string
}

export interface OAuthStatus {
  configured: boolean
  client_id: string
  open_id: string
}

// 用户与认证
export type Role = 'super_admin' | 'admin' | 'leader' | 'member'
export type PermissionCode =
  | 'online.summary.view' | 'online.raw.view' | 'online.raw.edit'
  | 'online.raw.row_manage' | 'visit.summary.view'
  | 'personnel.basic.view' | 'personnel.sensitive.view' | 'community.view'
  | 'notification.view' | 'preferences.manage' | 'sync.trigger'
  | 'report.config.manage' | 'visit.import'
  | 'worklog.manage' | 'attendance.manage' | 'personnel.manage'
  | 'community.manage' | 'user.manage' | 'permission.manage'
  | 'announcement.manage' | 'system.manage' | 'ops.manage'
  | 'police.dispatch.manage' | 'police.address.manage'
export type TableDisplayMode = 'table' | 'card'
export type ReportColumnMode = 'two' | 'three'
export type MobileNavigationMode = 'sidebar' | 'dock'
export type ThemeMode = 'light' | 'dark' | 'system'
export type MobileNavigationGroupId = 'workspace' | 'resources' | 'system'
export type MobileNavigationItemId =
  | 'online_summary'
  | 'online_query'
  | 'visit_summary'
  | 'data_upload'
  | 'work_log'
  | 'grid_members'
  | 'communities'
  | 'police_addresses'
  | 'users'
  | 'permission_groups'
  | 'settings'
  | 'operations'

export interface MobileDockGroupConfig {
  id: MobileNavigationGroupId
  items: MobileNavigationItemId[]
}

export interface MobileDockConfig {
  groups: MobileDockGroupConfig[]
}

export interface UserPreferences {
  table_display_mode?: TableDisplayMode
  report_column_mode?: ReportColumnMode
  mobile_navigation_mode?: MobileNavigationMode
  mobile_dock_config?: MobileDockConfig
  theme_mode?: ThemeMode
}

export interface User extends UserPreferences {
  id: number
  username: string
  display_name: string
  role: Role
  table_display_mode: TableDisplayMode
  report_column_mode: ReportColumnMode
  mobile_navigation_mode: MobileNavigationMode
  mobile_dock_config: MobileDockConfig
  theme_mode: ThemeMode
  permissions: PermissionCode[]
  data_scope: 'all' | 'own_department'
  permission_scopes: Partial<Record<PermissionCode, 'all' | 'own_department'>>
  member: { id: number; name: string; position: string } | null
  department: {
    id: number
    name: string
    type: 'community' | 'internal'
    community_name: string | null
  } | null
  departments: Array<{
    id: number
    name: string
    type: 'community' | 'internal'
    community_name: string | null
  }>
  community_names: string[]
  permission_group: { id: number | null; code: string; name: string }
  permission_groups: Array<{ id: number | null; code: string; name: string }>
  password_is_temporary: boolean
  session_policy: {
    idle_timeout_minutes: number
    warning_seconds: number
    last_activity_at: string
    absolute_expires_at: string
    server_time: string
  }
  created_at?: string
  updated_at?: string
}

export function getUserDisplayName(
  user: Pick<User, 'username' | 'display_name' | 'member'>,
): string {
  return user.display_name?.trim() || user.member?.name || user.username
}

export function hasPermission(
  user: User | null | undefined,
  permission: PermissionCode,
): boolean {
  return Boolean(user?.permissions?.includes(permission))
}

export const ROLE_LABELS: Record<string, string> = {
  super_admin: '超级管理员',
  admin: '管理员',
  leader: '组长',
  member: '组员',
}

// 工作日志
export type WorkLogFieldType =
  | 'number'
  | 'decimal'
  | 'percent'
  | 'text'
  | 'textarea'
  | 'table'

export interface WorkLogColumn {
  key?: string
  label: string
  type?: Exclude<WorkLogFieldType, 'table'>
  width?: number
  required?: boolean
  children?: WorkLogColumn[]
}

export interface WorkLogField {
  id: string
  label: string
  type: WorkLogFieldType
  source: 'system' | 'manual' | 'derived'
  required: boolean
  width?: number
  precision?: number
  source_key?: string
  help?: string
  columns?: WorkLogColumn[]
  row_mode?: 'detail' | 'community' | 'fixed' | 'system'
  community_key?: string
  fixed_rows?: Array<Record<string, unknown>>
}

export type WorkLogBlock =
  | {
      type: 'heading'
      title: string
      level: number
    }
  | {
      type: 'sentence'
      title?: string
      segments: Array<string | WorkLogField>
    }
  | {
      type: 'textarea'
      field: WorkLogField
      rows?: number
    }
  | {
      type: 'table'
      field: WorkLogField
      help?: string
    }

export interface WorkLogSection {
  id: string
  title: string
  description?: string
  blocks: WorkLogBlock[]
}

export interface WorkLogSchema {
  template_version: string
  document_title: string
  report_types: Array<{
    value: 'daily' | 'weekly' | 'monthly'
    label: string
    enabled: boolean
    hint?: string
  }>
  sections: WorkLogSection[]
}

export interface WorkLogSystemSnapshot {
  business_date: string
  issue_date: string
  month: number
  filename_prefix: string
  communities?: string[]
  community_grid_member_counts?: Record<string, number>
  legacy_v1?: Record<string, unknown>
  values: Record<string, unknown>
  sources: Record<string, {
    label: string
    available: boolean
    message: string
  }>
}

export interface WorkLogDraft {
  id: number
  report_type: 'daily'
  business_date: string
  owner: { id: number; username: string; display_name: string }
  can_edit: boolean
  template_version: string
  system_snapshot: WorkLogSystemSnapshot
  manual_values: Record<string, unknown>
  override_values: Record<string, unknown>
  version: number
  last_export_at: string | null
  created_at: string
  updated_at: string
}

export interface WorkLogDraftSummary {
  id: number
  report_type: 'daily'
  business_date: string
  owner: { id: number; username: string; display_name: string }
  creator: { id: number; username: string; display_name: string }
  template_version: string
  version: number
  last_export_at: string | null
  created_at: string
  updated_at: string
}

export interface WorkLogMissingItem {
  field_id: string
  label: string
  section: string
  reason: string
}
