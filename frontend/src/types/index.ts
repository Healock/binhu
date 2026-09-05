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

export interface AppNotification {
  id: number
  source: 'announcement' | 'personal'
  category: string
  severity: 'error' | 'warning' | 'info'
  title: string
  content: string
  related_task_id: number | null
  action_path: string | null
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
  memory_cache_bytes?: number
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
  sync_timezone: string
  sync_daily_counts: Array<{
    business_date: string
    total: number
    success: number
    partial: number
    failed: number
    unfinished: number
    manual: number
    scheduled: number
  }>
  txdocs_request_usage: {
    daily_limit: number
    timezone: string
    today: {
      business_date: string
      attempts: number
      success: number
      failure: number
      retries: number
      quota_exhausted_responses: number
      estimated_remaining: number
    }
    metering_started_at: string | null
    today_coverage_complete: boolean
    daily: Array<{
      business_date: string
      attempts: number
      success: number
      failure: number
      retries: number
      quota_exhausted_responses: number
      estimated_remaining: number
    }>
    today_breakdown: Array<{
      source: string
      endpoint: string
      method: string
      attempts: number
      success: number
      failure: number
      retries: number
    }>
  }
  photo_sheet_outbox: {
    pending: number
    retry: number
    paused: number
    max_attempt_count: number
    error: string | null
  }
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

export type OpsPerformanceState = 'normal' | 'busy' | 'congested' | 'recovering' | 'warming_up'

export interface OpsPerformanceSnapshot {
  generated_at: string
  monitoring_started_at: string
  window_minutes: number
  state: OpsPerformanceState
  state_label: string
  summary: {
    requests: number
    average_ms: number
    p50_ms: number
    p95_ms: number
    p99_ms: number
    max_ms: number
    errors_5xx: number
    error_rate: number
    conflicts_409: number
    timeouts: number
    cancelled: number
    inflight_peak: number
    requests_per_minute: number
    requests_per_second: number
    inflight_current: number
    inflight_peak_since_start: number
  }
  event_loop: {
    current_ms: number
    average_ms: number
    max_ms: number
  }
  signals: Array<{
    level: 'warning' | 'critical'
    code: string
    title: string
    detail: string
    recommended_action: string
    action_tab: string
  }>
  timeline: Array<{
    bucket_at: string
    requests: number
    average_ms: number
    p50_ms: number
    p95_ms: number
    p99_ms: number
    errors_5xx: number
    conflicts_409: number
    timeouts: number
    cancelled: number
    inflight_peak: number
  }>
  endpoint_groups: Array<{
    group: string
    group_label: string
    method: string
    route: string
    requests: number
    average_ms: number
    p50_ms: number
    p95_ms: number
    p99_ms: number
    errors_5xx: number
    error_rate: number
    conflicts_409: number
    timeouts: number
  }>
  database: {
    pools: Array<{
      name: string
      size: number
      used: number
      free: number
      max_size: number
      usage_percent: number
    }>
    mysql: {
      connected: boolean
      connections?: number
      max_connections?: number
      threads_running?: number
      lock_waits?: number
      slow_queries?: number
      error?: string
    }
  }
  background: {
    active_count: number
    queued_count: number
    running_count: number
    attention_count: number
    oldest_active_seconds: number
    occupancy_score: number
    categories: Array<{
      category: string
      active: number
      queued: number
      running: number
    }>
    unavailable_sources: string[]
    online_projection?: {
      queued_count: number
      running_count: number
      succeeded_count: number
      skipped_count: number
      failed_count: number
      oldest_wait_seconds: number
      enqueue_rate_1m: number
      process_rate_1m: number
      claimed_count: number
      coalesced_count: number
      processed_key_count: number
      revision_skipped_count: number
      split_retry_count: number
      lock_split_count: number
      retry_count: number
      micro_batch_size: number
      average_batch_size: number
      batch_p50_ms: number
      batch_p95_ms: number
      max_duration_ms: number
      recent_error_code: string
    }
    diagnostic_capture?: {
      expected_response_count: number
      expected_by_status: Record<string, number>
      captured_incident_count: number
      suppressed_duplicate_count: number
    }
    runtime_telemetry?: {
      legacy_metadata_query_count?: number
    }
  }
}

export interface DiagnosticJob {
  job_id: string
  mode: string
  status: string
  task_id: string | null
  page_url: string
  error_code: string
  error_message: string
  request_summary: Record<string, unknown>
  created_at: string | null
  finished_at: string | null
}

export interface DiagnosticReport {
  report_id: string
  overall_status: string
  summary: Array<{ code: string; status: string; summary: string }>
  technical: Array<Record<string, unknown>>
  created_at: string | null
  finished_at: string | null
}

export interface AuditEvent {
  id: number
  user_id: number | null
  username: string
  actor_name: string
  actor_account: string
  action: string
  action_label: string
  target_type: string
  target_name: string
  target_display: string
  result: string
  result_label: string
  detail: Record<string, unknown> | null
  detail_items: AuditDetailItem[]
  ip_address: string
  user_agent: string
  created_at: string
}

export interface AuditDetailItem {
  key: string
  label: string
  value: string
}

export interface AuditActionOption {
  value: string
  label: string
}

// 用户与认证
export type Role = 'super_admin' | 'admin' | 'leader' | 'member'
export type PermissionCode =
  | 'online.summary.view' | 'online.raw.view' | 'online.raw.edit'
  | 'online.raw.row_manage' | 'visit.summary.view'
  | 'online.task.manage'
  | 'personnel.basic.view' | 'personnel.sensitive.view' | 'community.view'
  | 'notification.view' | 'preferences.manage' | 'sync.trigger'
  | 'report.config.manage' | 'visit.import'
  | 'visit.source.manage'
  | 'worklog.manage' | 'attendance.manage' | 'personnel.manage'
  | 'community.manage' | 'user.manage' | 'permission.manage'
  | 'announcement.manage' | 'system.manage' | 'ops.manage'
  | 'presence.detail.view'
  | 'police.dispatch.manage' | 'police.address.manage'
  | 'registry.property.view' | 'registry.property.manage'
  | 'registry.watch.view' | 'registry.watch.manage' | 'registry.import.manage'
  | 'workflow.ticket.create' | 'workflow.ticket.view'
  | 'workflow.ticket.handle' | 'workflow.ticket.manage'
  | 'workflow.config.manage' | 'workflow.attachment.view'
  | 'venue.view' | 'venue.manage' | 'venue.export'
export type TableDisplayMode = 'table' | 'card'
export type TaskDisplayMode = 'table' | 'card'
export type ReportColumnMode = 'two' | 'three'
export type MobileNavigationMode = 'sidebar' | 'dock'
export type ThemeMode = 'light' | 'dark' | 'system'

export interface VisitSourceRun {
  id: number
  source: 'detail' | 'rating'
  trigger_source: 'manual' | 'scheduled'
  status: 'preview' | 'pending_confirmation' | 'confirmed' | 'superseded' | 'kept' | 'failed'
  start_date: string | null
  end_date: string | null
  response_business_date: string | null
  source_page: string
  record_count: number
  valid_count: number
  issue_count: number
  issues?: string[]
  error_code?: string | null
  error_message?: string | null
  created_at?: string | null
  diff?: {
    inserted: number
    updated: number
    unchanged: number
    deleted: number
    unmatched: number
    ambiguous: number
  }
}
export type MobileNavigationGroupId =
  | 'workspace'
  | 'tasks'
  | 'summaries'
  | 'resources'
  | 'system'
export type MobileNavigationItemId =
  | 'dashboard'
  | 'help'
  | 'task_flow_lab'
  | 'online_summary'
  | 'online_query'
  | 'flow_tasks'
  | 'visit_summary'
  | 'code_summary'
  | 'data_upload'
  | 'work_log'
  | 'police_tasks'
  | 'police_analysis'
  | 'photo_tasks'
  | 'workflow_tickets'
  | 'registry'
  | 'workflow_config'
  | 'grid_members'
  | 'communities'
  | 'police_addresses'
  | 'users'
  | 'permission_groups'
  | 'settings'
  | 'operations'
  | 'venue'

export interface MobileDockGroupConfig {
  id: MobileNavigationGroupId
  items: MobileNavigationItemId[]
}

export interface MobileDockConfig {
  version?: number
  groups: MobileDockGroupConfig[]
}

export interface UserPreferences {
  table_display_mode?: TableDisplayMode
  task_display_mode?: TaskDisplayMode
  report_column_mode?: ReportColumnMode
  mobile_navigation_mode?: MobileNavigationMode
  mobile_dock_config?: MobileDockConfig
  theme_mode?: ThemeMode
}

export interface User extends UserPreferences {
  id: number
  username: string
  display_name: string
  avatar_url?: string | null
  role: Role
  table_display_mode: TableDisplayMode
  task_display_mode: TaskDisplayMode
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

export interface PresenceUser {
  id: number
  display_name: string
  avatar_url: string | null
  position: string
  department: string | null
  last_seen_at: string | null
}

export interface PresenceHeartbeatResponse {
  online_count: number
  server_time: string
  online_window_seconds: number
}

export interface PresenceUsersResponse {
  online_count: number
  online_window_seconds: number
  users: PresenceUser[]
}

export interface WorkContributionDay {
  date: string
  count: number
}

export interface WorkContributionCategory {
  type: 'online_task_update' | 'police_dispatch_review' | 'work_log' | string
  label: string
  count: number
}

export interface WorkContributionSummary {
  total: number
  active_days: number
  longest_streak: number
  days: WorkContributionDay[]
  categories: WorkContributionCategory[]
}

export interface PublicProfileSummary {
  id: number
  display_name: string
  position: string
  departments: string[]
  community_names: string[]
  joined_at: string | null
  contribution: WorkContributionSummary
}

export interface PublicProfile extends PublicProfileSummary {
  year: number
  available_years: number[]
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
