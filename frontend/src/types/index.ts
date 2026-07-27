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
  category: string
  severity: 'error' | 'warning' | 'info'
  title: string
  content: string
  related_task_id: number | null
  is_read: boolean
  created_at: string
  read_at: string | null
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

export interface User {
  id: number
  username: string
  role: Role
  created_at?: string
  updated_at?: string
}

export const ROLE_LABELS: Record<string, string> = {
  super_admin: '超级管理员',
  admin: '管理员',
  leader: '组长',
  member: '组员',
}
