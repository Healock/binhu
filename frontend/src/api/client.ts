import axios from 'axios'
import type {
  Spreadsheet, SpreadsheetCreate, StatsResponse, StatsItem,
  SyncStatus, SyncTriggerResponse, OAuthConfig, OAuthStatus,
} from '../types'

const api = axios.create({
  baseURL: '/api',
  timeout: 30000,
  withCredentials: true,
})

// 401 拦截器：跳转登录页
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401 && !window.location.pathname.includes('/login')) {
      window.location.href = '/login'
    }
    return Promise.reject(error)
  }
)

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

// ---- Stats / 日报 ----
export async function getReportTypes(): Promise<{ data: string[]; implemented: string[] }> {
  const { data } = await api.get('/stats/types')
  return data
}

export async function buildReport(params: { date?: string; parser_type?: string }): Promise<{ message: string; implemented: boolean; inspector_rows?: number; community_rows?: number; date?: string }> {
  const { data } = await api.post('/stats/build', null, { params })
  return data
}

export async function getReport(date: string, parser_type?: string): Promise<any> {
  const { data } = await api.get('/stats/report', { params: { report_date: date, parser_type: parser_type || '全链条' } })
  return data
}

export async function getReportRange(startDate: string, endDate: string, parserType: string): Promise<any> {
  const { data } = await api.get('/stats/report_range', { params: { start_date: startDate, end_date: endDate, parser_type: parserType } })
  return data
}

export async function listReports(): Promise<{ date: string; type: string; method: string; generated_at: string }[]> {
  const { data } = await api.get('/stats/reports')
  return data.data
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

export async function queryData(params: {
  type: string
  source?: string
  page?: number
  page_size?: number
  keyword?: string
  sort_by?: string
  sort_order?: string
  filters?: Record<string, string[]>
}): Promise<{ data: Record<string, string>[]; total: number; page: number; page_size: number; columns: string[] }> {
  const { data } = await api.get(`/query/${params.type}`, {
    params: {
      source: params.source || 'online',
      page: params.page || 1,
      page_size: params.page_size || 50,
      keyword: params.keyword,
      sort_by: params.sort_by,
      sort_order: params.sort_order,
      filters: params.filters ? JSON.stringify(params.filters) : undefined,
    },
  })
  return data
}

// ---- Grid Members ----
export interface GridMember {
  id: number
  name: string
  community: string
  phone: string
  notes: string
  status: string
}

export async function listGridMembers(params: {
  keyword?: string
  community?: string
  page?: number
  page_size?: number
}): Promise<{ data: GridMember[]; total: number; page: number; page_size: number }> {
  const { data } = await api.get('/grid-members', { params })
  return data
}

export async function getGridCommunities(): Promise<{ id: number; name: string; grid_count: number }[]> {
  const { data } = await api.get('/grid-members/communities')
  return data.data
}

export async function addGridCommunity(name: string): Promise<void> {
  await api.post('/grid-members/communities', null, { params: { name } })
}

export async function deleteGridCommunity(id: number): Promise<void> {
  await api.delete(`/grid-members/communities/${id}`)
}

export async function importCommunitiesFromData(): Promise<{ new_count: number; new_names: string[] }> {
  const { data } = await api.post('/grid-members/communities/import-from-data')
  return data
}

export async function createGridMember(payload: { name: string; community?: string; phone?: string; notes?: string; status?: string }): Promise<void> {
  await api.post('/grid-members', payload)
}

export async function updateGridMember(id: number, payload: { community?: string; phone?: string; notes?: string; status?: string }): Promise<void> {
  await api.put(`/grid-members/${id}`, payload)
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
