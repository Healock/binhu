import { resolveDesktopBridge, type ResidenceProbeResult } from '../desktop/bridge.ts'

export interface OfflineResidenceAccount {
  community_id: number | null
  community_name: string
  username: string
  community_code: string
}

export interface OfflineMacAuthorizedCommunity {
  community_id: number | null
  community_name: string
}

export interface OfflineMacProbeSnapshot {
  checked_at: string
  authorized_communities: OfflineMacAuthorizedCommunity[]
  tested_community_count: number
  probe_version?: 1
  rejected_community_count?: number
  probe_failures?: Array<{ community_id: number | null; community_name: string; status: string; error_code?: string }>
}

export interface OfflineResidenceConfig {
  enabled: boolean
  base_url: string
  username: string
  password: string
  mac_service_url: string
  mac_address: string
  mac_addresses: string[]
  mac_probe_results: Record<string, OfflineMacProbeSnapshot>
  timeout_seconds: number
  community_codes: string[]
  login_community_ids: number[]
  login_community_names: string[]
  accounts: OfflineResidenceAccount[]
}

export interface OnlineResidenceConfigSnapshot {
  enabled?: boolean
  base_url?: string
  mac_service_url?: string
  timeout_seconds?: number
  community_codes?: string[]
  login_community_ids?: number[]
  login_community_names?: string[]
  login_community_codes?: string[]
  login_community_usernames?: string[]
}

export interface OfflineResidenceQueryResult {
  status: string
  error?: string
  diagnostics?: OfflineResidenceDiagnosticEvent[]
}

export interface OfflineResidenceAddressResult extends OfflineResidenceQueryResult {
  /** Only the whitelisted registration address fields; never the raw upstream row. */
  registered_address?: string
}

export type OfflineResidenceDiagnosticStage = 'captcha' | 'login' | 'search_resident' | 'search_floating'

/** Redacted metadata for local diagnostics; never contains identity, token, or response text. */
export interface OfflineResidenceDiagnosticEvent {
  stage: OfflineResidenceDiagnosticStage
  error_code: string
  http_status?: number
  business_code?: string
  success?: boolean
  result_type?: string
  message_category?: string
}

export interface OfflineMacProbeResult extends OfflineMacAuthorizedCommunity {
  allowed: boolean
  status: ResidenceProbeResult['status']
  error_code?: string
}

const SEARCH_RESIDENT_PATH = '/szjzz/searchIsck'
const SEARCH_FLOATING_PATH = '/szjzz/searchzzrk'
const LOGIN_PATH = '/sys/login'
const CAPTCHA_PATH_PREFIX = '/sys/randomImage/'
const STORAGE_KEY = 'binhu_offline_residence_config_v1'
const LOCAL_MAC_SERVICE_URL = 'http://127.0.0.1:23333'

class ResidenceProbeError extends Error {
  readonly status: ResidenceProbeResult['status']
  readonly code: string

  constructor(status: ResidenceProbeResult['status'], code: string) {
    super(code)
    this.name = 'ResidenceProbeError'
    this.status = status
    this.code = code
  }
}

export const DEFAULT_OFFLINE_RESIDENCE_CONFIG: OfflineResidenceConfig = {
  enabled: true,
  base_url: '',
  username: '',
  password: '',
  mac_service_url: 'http://127.0.0.1:23333',
  mac_address: '',
  mac_addresses: [],
  mac_probe_results: {},
  timeout_seconds: 15,
  community_codes: [],
  login_community_ids: [],
  login_community_names: [],
  accounts: [],
}

function normalizeAccount(value: Partial<OfflineResidenceAccount>): OfflineResidenceAccount {
  const rawId = Number(value.community_id)
  return {
    community_id: Number.isInteger(rawId) && rawId > 0 ? rawId : null,
    community_name: String(value.community_name || '').trim(),
    username: String(value.username || '').trim(),
    community_code: String(value.community_code || '').trim().toUpperCase(),
  }
}

export function loadOfflineResidenceConfig(): OfflineResidenceConfig {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return { ...DEFAULT_OFFLINE_RESIDENCE_CONFIG }
    const parsed = JSON.parse(raw) as Partial<OfflineResidenceConfig>
    const storedAccounts = Array.isArray(parsed.accounts)
      ? parsed.accounts.map(account => normalizeAccount(account)).filter(account => account.username || account.community_id)
      : []
    const legacyUsername = typeof parsed.username === 'string' ? parsed.username.trim() : ''
    const legacyCodes = Array.isArray(parsed.community_codes) ? parsed.community_codes.map(String).filter(Boolean) : []
    const accounts = storedAccounts.length ? storedAccounts : legacyUsername ? [normalizeAccount({
      username: legacyUsername,
      community_code: legacyCodes[0] || '',
    })] : []
    const activeMac = normalizeMacOrEmpty(parsed.mac_address)
    const macAddresses = Array.from(new Set([
      activeMac,
      ...(Array.isArray(parsed.mac_addresses) ? parsed.mac_addresses.map(normalizeMacOrEmpty) : []),
    ].filter(Boolean)))
    const macProbeResults = parsed.mac_probe_results && typeof parsed.mac_probe_results === 'object'
      ? Object.fromEntries(Object.entries(parsed.mac_probe_results).flatMap(([mac, snapshot]) => {
          const normalized = normalizeMacOrEmpty(mac)
          if (!normalized || !snapshot || typeof snapshot !== 'object') return []
          const value = snapshot as Partial<OfflineMacProbeSnapshot>
          return [[normalized, {
            checked_at: typeof value.checked_at === 'string' ? value.checked_at : '',
            tested_community_count: Number.isInteger(value.tested_community_count) ? Number(value.tested_community_count) : 0,
            ...(value.probe_version === 1 ? { probe_version: 1 as const } : {}),
            authorized_communities: Array.isArray(value.authorized_communities)
              ? value.authorized_communities.map(item => ({
                  community_id: Number.isInteger(item?.community_id) && Number(item.community_id) > 0 ? Number(item.community_id) : null,
                  community_name: String(item?.community_name || '').trim(),
                })).filter(item => item.community_name)
              : [],
            rejected_community_count: Number.isInteger(value.rejected_community_count) ? Number(value.rejected_community_count) : 0,
            probe_failures: Array.isArray(value.probe_failures)
              ? value.probe_failures.map(item => ({
                  community_id: Number.isInteger(item?.community_id) && Number(item.community_id) > 0 ? Number(item.community_id) : null,
                  community_name: String(item?.community_name || '').trim(),
                  status: String(item?.status || 'network_error'),
                  error_code: item?.error_code ? String(item.error_code) : undefined,
                })).filter(item => item.community_name)
              : [],
          } satisfies OfflineMacProbeSnapshot]]
        }))
      : {}
    return {
      ...DEFAULT_OFFLINE_RESIDENCE_CONFIG,
      ...parsed,
      mac_service_url: LOCAL_MAC_SERVICE_URL,
      username: typeof parsed.username === 'string' ? parsed.username.trim() : '',
      mac_address: activeMac,
      mac_addresses: macAddresses,
      mac_probe_results: macProbeResults,
      community_codes: Array.isArray(parsed.community_codes) ? parsed.community_codes.map(String).filter(Boolean) : [],
      login_community_ids: Array.isArray(parsed.login_community_ids)
        ? Array.from(new Set(parsed.login_community_ids.filter(id => Number.isInteger(id) && id > 0))).sort((a, b) => a - b)
        : [],
      login_community_names: Array.isArray(parsed.login_community_names)
        ? parsed.login_community_names.map(String).filter(Boolean)
        : [],
      accounts,
      timeout_seconds: Math.min(120, Math.max(1, Number(parsed.timeout_seconds || 15))),
    }
  } catch {
    return { ...DEFAULT_OFFLINE_RESIDENCE_CONFIG }
  }
}

export function saveOfflineResidenceConfig(config: OfflineResidenceConfig): void {
  const accounts = config.accounts.map(account => normalizeAccount(account))
  const activeMac = normalizeMacOrEmpty(config.mac_address)
  const macAddresses = Array.from(new Set([
    activeMac,
    ...config.mac_addresses.map(normalizeMacOrEmpty),
  ].filter(Boolean)))
  const macProbeResults = Object.fromEntries(
    Object.entries(config.mac_probe_results).filter(([mac]) => macAddresses.includes(mac)),
  )
  localStorage.setItem(STORAGE_KEY, JSON.stringify({
    ...config,
    base_url: config.base_url.trim().replace(/\/+$/, ''),
    mac_service_url: LOCAL_MAC_SERVICE_URL,
    mac_address: activeMac,
    mac_addresses: macAddresses,
    mac_probe_results: macProbeResults,
    login_community_ids: Array.from(new Set(config.login_community_ids.filter(id => Number.isInteger(id) && id > 0))).sort((a, b) => a - b),
    login_community_names: config.login_community_names.map(item => item.trim()).filter(Boolean),
    accounts,
    // Retain a single-account projection so older installed clients can still
    // read the device-local configuration without widening its scope.
    username: accounts.length === 1 ? accounts[0].username : '',
    community_codes: accounts.length === 1 && accounts[0].community_code ? [accounts[0].community_code] : [],
  }))
}

/** Merge a successful authorized online read into the device-local offline config. */
export function cacheOnlineResidenceConfig(snapshot: OnlineResidenceConfigSnapshot): OfflineResidenceConfig {
  const current = loadOfflineResidenceConfig()
  if (
    typeof snapshot.base_url !== 'string' || !snapshot.base_url.trim()
  ) {
    return current
  }
  const selectedIds = Array.isArray(snapshot.login_community_ids)
    ? Array.from(new Set(snapshot.login_community_ids.filter(id => Number.isInteger(id) && id > 0))).sort((a, b) => a - b)
    : null
  const selectedNames = Array.isArray(snapshot.login_community_names) ? snapshot.login_community_names.map(String) : []
  const selectedCodes = Array.isArray(snapshot.login_community_codes)
    ? snapshot.login_community_codes.map(code => String(code).trim().toUpperCase())
    : Array.isArray(snapshot.community_codes) ? snapshot.community_codes.map(code => String(code).trim().toUpperCase()) : []
  const selectedUsernames = Array.isArray(snapshot.login_community_usernames)
    ? snapshot.login_community_usernames.map(username => String(username).trim())
    : []
  const currentById = new Map(current.accounts.filter(account => account.community_id).map(account => [account.community_id, account]))
  const legacyAccount = current.accounts.length === 1 && current.accounts[0].community_id == null
    ? current.accounts[0]
    : null
  const scopedAccounts = selectedIds === null ? current.accounts : selectedIds.map((id, index) => {
    const existing = currentById.get(id)
    return normalizeAccount({
      community_id: id,
      community_name: selectedNames[index] || existing?.community_name || '',
      username: selectedUsernames[index] || existing?.username || (selectedIds.length === 1 ? legacyAccount?.username : '') || '',
      community_code: selectedCodes[index] || existing?.community_code || (selectedIds.length === 1 ? legacyAccount?.community_code : '') || '',
    })
  })
  const next: OfflineResidenceConfig = {
    ...current,
    ...(typeof snapshot.enabled === 'boolean' ? { enabled: snapshot.enabled } : {}),
    ...(typeof snapshot.base_url === 'string' ? { base_url: snapshot.base_url } : {}),
    mac_service_url: LOCAL_MAC_SERVICE_URL,
    ...(typeof snapshot.timeout_seconds === 'number' ? { timeout_seconds: snapshot.timeout_seconds } : {}),
    ...(selectedIds !== null
      ? {
          login_community_ids: selectedIds,
          login_community_names: selectedNames.filter(Boolean),
          accounts: scopedAccounts,
        }
      : {}),
  }
  saveOfflineResidenceConfig(next)
  return loadOfflineResidenceConfig()
}

function baseUrl(config: OfflineResidenceConfig): string {
  const value = config.base_url.trim().replace(/\/+$/, '')
  let parsed: URL
  try { parsed = new URL(value) } catch { throw new Error('居住证接口地址格式无效') }
  if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password || parsed.search || parsed.hash || parsed.pathname.includes('..')) {
    throw new Error('居住证接口地址格式无效')
  }
  return value
}

function residenceUrl(base: string, path: string): string {
  return `${base}${path.startsWith('/') ? path : `/${path}`}`
}

const MAC_ADDRESS_RE = /^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$/

/** Normalize the formats accepted by the local compatibility service. */
export function normalizeMacAddress(value: string): string {
  const compact = String(value || '').trim().replace(/[.\-:\s]/g, '').toUpperCase()
  if (!/^[0-9A-F]{12}$/.test(compact)) throw new Error('MAC 地址必须是 12 位十六进制字符')
  const normalized = compact.match(/.{2}/g)?.join(':') || ''
  const firstOctet = Number.parseInt(normalized.slice(0, 2), 16)
  if (!MAC_ADDRESS_RE.test(normalized) || normalized === '00:00:00:00:00:00' || (firstOctet & 1) === 1) {
    throw new Error('MAC 地址格式无效')
  }
  return normalized
}

function normalizeMacOrEmpty(value: unknown): string {
  try {
    return normalizeMacAddress(String(value || ''))
  } catch {
    return ''
  }
}

function macServiceUrl(config: OfflineResidenceConfig): string {
  const value = config.mac_service_url.trim().replace(/\/+$/, '')
  let parsed: URL
  try { parsed = new URL(value) } catch { throw new Error('MAC 服务地址格式无效') }
  if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error('MAC 服务地址格式无效')
  }
  return value
}

function organizationCodesMatch(expected: string, actual: string): boolean {
  const left = expected.trim().toUpperCase()
  const right = actual.trim().toUpperCase()
  if (!left || !right) return true
  return left === right || (left.length >= 6 && right.startsWith(left)) || (right.length >= 6 && left.startsWith(right))
}

async function jsonRequest(config: OfflineResidenceConfig, url: string, init: RequestInit): Promise<any> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), config.timeout_seconds * 1000)
  try {
    const response = await fetch(url, {
      ...init,
      credentials: 'omit',
      mode: 'cors',
      signal: controller.signal,
    })
    if (!response.ok) throw new ResidenceRequestError('http_error', response.status)
    try {
      return await response.json()
    } catch {
      throw new ResidenceRequestError('invalid_response')
    }
  } finally {
    window.clearTimeout(timer)
  }
}

class ResidenceRequestError extends Error {
  readonly code: string
  readonly httpStatus?: number

  constructor(code: string, httpStatus?: number) {
    super(code)
    this.name = 'ResidenceRequestError'
    this.code = code
    this.httpStatus = httpStatus
  }
}

function classifyRequestError(error: unknown): string {
  if (error instanceof ResidenceRequestError || (error && typeof error === 'object' && 'code' in error && typeof (error as { code?: unknown }).code === 'string')) return String((error as { code: string }).code)
  const value = error instanceof Error ? error.message : String(error || '')
  if (['network_error', 'timeout', 'invalid_response', 'response_too_large', 'http_error', 'config_error'].includes(value)) return value
  if (/aborted|timeout/i.test(value)) return 'timeout'
  if (/failed to fetch|network/i.test(value)) return 'network_error'
  return 'request_error'
}

interface ResidenceJsonResponse {
  payload: any
  httpStatus: number
}

async function residenceJsonRequest(
  config: OfflineResidenceConfig,
  path: string,
  init: RequestInit,
): Promise<ResidenceJsonResponse> {
  const method = init.method === 'GET' ? 'GET' : 'POST'
  const headers = Object.fromEntries(new Headers(init.headers).entries())
  const body = typeof init.body === 'string' ? init.body : undefined
  const desktop = resolveDesktopBridge()
  if (desktop?.requestResidenceApi) {
    try {
      const response = await desktop.requestResidenceApi({
        baseUrl: baseUrl(config),
        path,
        method,
        headers,
        body,
        timeoutSeconds: config.timeout_seconds,
      })
      if (response.statusCode < 200 || response.statusCode >= 300) throw new ResidenceRequestError('http_error', response.statusCode)
      return { payload: response.payload, httpStatus: response.statusCode }
    } catch (error) {
      if (error instanceof ResidenceRequestError || (error && typeof error === 'object' && 'code' in error)) throw error
      throw new ResidenceRequestError(classifyRequestError(error))
    }
  }
  try {
    return { payload: await jsonRequest(config, residenceUrl(baseUrl(config), path), init), httpStatus: 200 }
  } catch (error) {
    if (error instanceof ResidenceRequestError || (error && typeof error === 'object' && 'code' in error)) throw error
    throw new ResidenceRequestError(classifyRequestError(error))
  }
}

export async function readMacAddress(config: OfflineResidenceConfig): Promise<string> {
  const payload = await jsonRequest(config, macServiceUrl(config), { method: 'GET' })
  try { return normalizeMacAddress(String(payload?.mac || '')) } catch { throw new Error('MAC 服务未返回有效设备地址') }
}

function authResponse(payload: any): boolean {
  const message = String(payload?.message || '').toLowerCase()
  return [401, 403].includes(Number(payload?.code)) || ['token', '登录失效', '未登录', '认证失败'].some(marker => message.includes(marker))
}

const IDENTITY_18_RE = /^\d{17}[0-9X]$/
const IDENTITY_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
const IDENTITY_CHECKS = ['1', '0', 'X', '9', '8', '7', '6', '5', '4', '3', '2']

/** Normalize and validate a PRC identity number before sending it upstream. */
export function normalizeResidenceIdentity(value: unknown): string {
  let identity = String(value ?? '').trim().replace(/^[\u0027\u2019]/, '').replace(/\s+/g, '').toUpperCase()
  if (/^\d{15}$/.test(identity)) {
    identity = `${identity.slice(0, 6)}19${identity.slice(6)}`
  }
  if (!IDENTITY_18_RE.test(identity)) return ''
  const year = Number(identity.slice(6, 10))
  const month = Number(identity.slice(10, 12))
  const day = Number(identity.slice(12, 14))
  const date = new Date(Date.UTC(year, month - 1, day))
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return ''
  const checksum = IDENTITY_CHECKS[IDENTITY_WEIGHTS.reduce((sum, weight, index) => sum + Number(identity[index]) * weight, 0) % 11]
  return checksum === identity[17] ? identity : ''
}

/**
 * The residence platform has returned the login organisation under several
 * equivalent field names over time. Keep the offline client aligned with the
 * server client and search nested login result objects before falling back to
 * the configured community code.
 */
export function extractResidenceOrganizationCode(value: any): string {
  const queue: any[] = [value]
  while (queue.length) {
    const current = queue.shift()
    if (current && typeof current === 'object') {
      for (const key of ['orgCode', 'org_code', 'departCode']) {
        const candidate = String(current[key] || '').trim()
        if (candidate.length >= 6 && /^\d{6}/.test(candidate)) return candidate
      }
      if (Array.isArray(current)) queue.push(...current)
      else queue.push(...Object.values(current))
    }
  }
  return ''
}

function payloadResultType(payload: any): string {
  if (payload === null) return 'null'
  if (payload === undefined) return 'missing'
  if (Array.isArray(payload)) return 'array'
  return typeof payload
}

function messageCategory(payload: any): string {
  const message = String(payload?.message || '').trim().toLowerCase()
  if (!message) return 'none'
  if (message.includes('没有查询到数据') || message.includes('no data')) return 'no_data'
  if (['token', '登录失效', '未登录', '认证失败', 'unauthorized'].some(marker => message.includes(marker))) return 'authentication'
  return 'other'
}

export function summarizeResidencePayload(payload: any, httpStatus = 200): Omit<OfflineResidenceDiagnosticEvent, 'stage' | 'error_code'> {
  return {
    http_status: httpStatus,
    business_code: payload && typeof payload === 'object' && payload.code !== undefined ? String(payload.code) : 'missing',
    success: typeof payload?.success === 'boolean' ? payload.success : undefined,
    result_type: payloadResultType(payload?.result),
    message_category: messageCategory(payload),
  }
}

function diagnosticFromPayload(stage: OfflineResidenceDiagnosticStage, errorCode: string, payload: any, httpStatus: number): OfflineResidenceDiagnosticEvent {
  return { stage, error_code: errorCode, ...summarizeResidencePayload(payload, httpStatus) }
}

function classify(payload: any): { state: 'registered' | 'not_found' | 'error'; status?: string; error?: string } {
  if (payload?.success === true && Number(payload?.code) === 200 && payload?.result && typeof payload.result === 'object' && !Array.isArray(payload.result)) {
    const code = String(payload.result.rysfzx || '').trim()
    return { state: 'registered', status: code === '1' ? '已注销' : code === '0' ? '未注销' : '状态待核对' }
  }
  if (payload?.success === false && Number(payload?.code) === 500 && payload?.result == null && String(payload?.message || '').includes('没有查询到数据')) {
    return { state: 'not_found' }
  }
  if (authResponse(payload)) return { state: 'error', error: 'authentication_expired' }
  if (payload && typeof payload === 'object' && typeof payload.success === 'boolean' && payload.code !== undefined) {
    return { state: 'error', error: payload.success === false ? 'floating_business_error' : 'floating_response_contract_changed' }
  }
  return { state: 'error', error: 'floating_response_contract_changed' }
}

function registeredAddress(payload: any): string {
  const raw = payload?.result
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return ''
  return [raw.jlx_dictText, raw.mph]
    .map(value => typeof value === 'string' ? value.trim() : '')
    .filter(Boolean)
    .join('')
}

export class OfflineResidenceClient {
  private readonly config: OfflineResidenceConfig
  private readonly tokens = new Map<string, { token: string; organizationCode: string }>()
  private readonly pendingLogins = new Map<string, Promise<{ token: string; organizationCode: string }>>()

  constructor(config: OfflineResidenceConfig) {
    this.config = { ...config, base_url: config.base_url.trim().replace(/\/+$/, '') }
  }

  private accountKey(account: OfflineResidenceAccount): string {
    return account.community_id
      ? `community_${account.community_id}:${account.username}:${account.community_code}`
      : `local_${account.community_code}:${account.username}`
  }

  private async authenticate(account: OfflineResidenceAccount, mac: string): Promise<{ token: string; organizationCode: string }> {
    const checkKey = String(Date.now())
    await residenceJsonRequest(this.config, `${CAPTCHA_PATH_PREFIX}${checkKey}`, { method: 'GET' })
    const payload = (await residenceJsonRequest(this.config, LOGIN_PATH, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json;charset=UTF-8' },
      body: JSON.stringify({
        username: account.username,
        password: this.config.password,
        mac,
        remember_me: true,
        captcha: '',
        checkKey,
        terminalType: 1,
      }),
    })).payload
    if (!payload?.success || !payload?.result?.token) throw new Error('居住证平台登录失败，请检查配置')
    const organizationCode = extractResidenceOrganizationCode(payload.result) || account.community_code
    if (!organizationCodesMatch(account.community_code, organizationCode)) {
      throw new Error('居住证账号返回的组织代码与所选社区不一致')
    }
    const session = { token: String(payload.result.token), organizationCode }
    return session
  }

  private async probeAccountAccess(account: OfflineResidenceAccount, mac: string): Promise<void> {
    const desktop = resolveDesktopBridge()
    if (desktop) {
      const result = await desktop.probeResidenceLogin({
        baseUrl: baseUrl(this.config),
        username: account.username,
        password: this.config.password,
        mac,
        timeoutSeconds: this.config.timeout_seconds,
        communityCode: account.community_code,
      })
      if (result.status !== 'allowed') throw new ResidenceProbeError(result.status, result.errorCode || 'login_rejected')
      return
    }
    await this.authenticate(account, mac)
  }

  private async login(account: OfflineResidenceAccount): Promise<{ token: string; organizationCode: string }> {
    const key = this.accountKey(account)
    const pending = this.pendingLogins.get(key)
    if (pending) return pending
    const request = (async () => {
      const session = await this.authenticate(account, await readMacAddress(this.config))
      this.tokens.set(key, session)
      return session
    })()
    this.pendingLogins.set(key, request)
    try { return await request } finally { this.pendingLogins.delete(key) }
  }

  async probeMacAccess(
    value: string,
    onProgress?: (completed: number, total: number, result: OfflineMacProbeResult) => void,
    shouldContinue?: () => boolean,
  ): Promise<OfflineMacProbeResult[]> {
    if (!this.config.password || !this.config.base_url) throw new Error('请先填写居住证接口地址和统一登录密码')
    const accounts = this.config.accounts.filter(account => account.username.trim()).slice(0, 12)
    if (!accounts.length) throw new Error('请先为社区填写完整登录账号')
    const mac = normalizeMacAddress(value)
    const results: OfflineMacProbeResult[] = []
    for (const account of accounts) {
      if (shouldContinue && !shouldContinue()) break
      let allowed = false
      let status: OfflineMacProbeResult['status'] = 'rejected'
      let error_code = ''
      try {
        await this.probeAccountAccess(account, mac)
        allowed = true
        status = 'allowed'
      } catch (error) {
        allowed = false
        if (error instanceof ResidenceProbeError) {
          status = error.status
          error_code = error.code
        } else if (/格式|配置|账号|密码/.test(error instanceof Error ? error.message : '')) {
          status = 'config_error'
          error_code = 'config_error'
        } else if (/登录失败|组织代码/.test(error instanceof Error ? error.message : '')) {
          status = 'rejected'
          error_code = 'login_rejected'
        } else if (error instanceof ResidenceRequestError || (error && typeof error === 'object' && 'code' in error)) {
          status = 'network_error'
          const requestCode = String((error as { code?: unknown }).code || 'request_failed')
          error_code = requestCode === 'network_error' ? 'request_failed' : requestCode
        } else if (error instanceof Error && error.message === 'invalid_response') {
          status = 'network_error'
          error_code = 'invalid_response'
        } else {
          status = 'network_error'
          error_code = 'request_failed'
        }
      }
      const result = {
        community_id: account.community_id,
        community_name: account.community_name || account.community_code || account.username,
        allowed,
        status,
        ...(error_code ? { error_code } : {}),
      }
      results.push(result)
      onProgress?.(results.length, accounts.length, result)
      if (results.length < accounts.length) await new Promise(resolve => window.setTimeout(resolve, 250))
    }
    return results
  }

  private async lookupWithToken(identity: string, session: { token: string; organizationCode: string }): Promise<{ result: ReturnType<typeof classify>; registeredAddress: string; diagnostics: OfflineResidenceDiagnosticEvent[] }> {
    const headers = {
      'X-Access-Token': session.token,
      tenant_id: '0',
      'Content-Type': 'application/json;charset=UTF-8',
    }
    const body = JSON.stringify({ sfzh: identity, xzqh: session.organizationCode.slice(0, 6) })
    const diagnostics: OfflineResidenceDiagnosticEvent[] = []
    try {
      const resident = await residenceJsonRequest(this.config, SEARCH_RESIDENT_PATH, { method: 'POST', headers, body })
      const known = resident.payload?.success === true && Number(resident.payload?.code) === 200 && resident.payload?.result == null
      const residentNotFound = classify(resident.payload).state === 'not_found'
      diagnostics.push(diagnosticFromPayload('search_resident', known ? 'resident_precheck_ok' : residentNotFound ? 'resident_no_data' : authResponse(resident.payload) ? 'authentication_expired' : 'resident_response_contract_changed', resident.payload, resident.httpStatus))
      if (authResponse(resident.payload)) return { result: { state: 'error', error: 'authentication_expired' }, registeredAddress: '', diagnostics }
      if (!known && classify(resident.payload).state !== 'not_found') {
        return { result: { state: 'error', error: 'resident_response_contract_changed' }, registeredAddress: '', diagnostics }
      }
    } catch (error) {
      const code = classifyRequestError(error)
      diagnostics.push({ stage: 'search_resident', error_code: code, ...(error instanceof ResidenceRequestError && error.httpStatus ? { http_status: error.httpStatus } : {}) })
      return { result: { state: 'error', error: code }, registeredAddress: '', diagnostics }
    }
    try {
      const floating = await residenceJsonRequest(this.config, SEARCH_FLOATING_PATH, { method: 'POST', headers, body })
      const result = classify(floating.payload)
      diagnostics.push(diagnosticFromPayload('search_floating', result.error || (result.state === 'registered' ? 'floating_registered' : 'floating_no_data'), floating.payload, floating.httpStatus))
      return { result, registeredAddress: result.state === 'registered' ? registeredAddress(floating.payload) : '', diagnostics }
    } catch (error) {
      const code = classifyRequestError(error)
      diagnostics.push({ stage: 'search_floating', error_code: code, ...(error instanceof ResidenceRequestError && error.httpStatus ? { http_status: error.httpStatus } : {}) })
      return { result: { state: 'error', error: code }, registeredAddress: '', diagnostics }
    }
  }

  private async lookupAcrossAccounts(identity: string, includeAddress: boolean): Promise<OfflineResidenceAddressResult> {
    if (!this.config.enabled) return { status: '查询未开启', error: 'disabled' }
    if (!this.config.password || !this.config.base_url || !this.config.accounts.length || this.config.accounts.some(account => !account.username)) {
      return { status: '配置不完整', error: 'config_incomplete' }
    }
    let lastError = ''
    let sawNotFound = false
    let sawRegistrationWithoutAddress = false
    const diagnostics: OfflineResidenceDiagnosticEvent[] = []
    for (const account of this.config.accounts) {
      try {
        const key = this.accountKey(account)
        let session = this.tokens.get(key) || await this.login(account)
        let attempt = await this.lookupWithToken(identity, session)
        diagnostics.push(...attempt.diagnostics)
        let result = attempt.result
        if (result.error === 'authentication_expired') {
          if (this.tokens.get(key)?.token === session.token) this.tokens.delete(key)
          session = this.tokens.get(key) || await this.login(account)
          attempt = await this.lookupWithToken(identity, session)
          diagnostics.push(...attempt.diagnostics)
          result = attempt.result
        }
        if (result.state === 'registered') {
          if (!includeAddress) return { status: result.status || '状态待核对', diagnostics }
          if (attempt.registeredAddress) return { status: result.status || '状态待核对', registered_address: attempt.registeredAddress, diagnostics }
          sawRegistrationWithoutAddress = true
          continue
        }
        if (result.state === 'not_found') sawNotFound = true
        else lastError = result.error || 'business_error'
      } catch (error) {
        lastError = classifyRequestError(error)
        diagnostics.push({ stage: 'login', error_code: lastError })
      }
    }
    if (sawRegistrationWithoutAddress) return { status: '登记地址待核对', error: 'address_unavailable', registered_address: '', diagnostics }
    if (sawNotFound && !lastError) return { status: '未登记', registered_address: '', diagnostics }
    return { status: '查询失败', error: lastError || 'not_found', registered_address: '', diagnostics }
  }

  async lookup(identity: string): Promise<OfflineResidenceQueryResult> {
    return this.lookupAcrossAccounts(identity, false)
  }

  async lookupRegistrationAddress(identity: string): Promise<OfflineResidenceAddressResult> {
    return this.lookupAcrossAccounts(identity, true)
  }
}
