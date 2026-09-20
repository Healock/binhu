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
}

export interface OfflineResidenceQueryResult {
  status: string
  error?: string
}

export interface OfflineMacProbeResult extends OfflineMacAuthorizedCommunity {
  allowed: boolean
}

const SEARCH_RESIDENT_PATH = '/szjzz/searchIsck'
const SEARCH_FLOATING_PATH = '/szjzz/searchzzrk'
const LOGIN_PATH = '/sys/login'
const CAPTCHA_PATH_PREFIX = '/sys/randomImage/'
const STORAGE_KEY = 'binhu_offline_residence_config_v1'
const LOCAL_MAC_SERVICE_URL = 'http://127.0.0.1:23333'

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
            authorized_communities: Array.isArray(value.authorized_communities)
              ? value.authorized_communities.map(item => ({
                  community_id: Number.isInteger(item?.community_id) && Number(item.community_id) > 0 ? Number(item.community_id) : null,
                  community_name: String(item?.community_name || '').trim(),
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
  const currentById = new Map(current.accounts.filter(account => account.community_id).map(account => [account.community_id, account]))
  const legacyAccount = current.accounts.length === 1 && current.accounts[0].community_id == null
    ? current.accounts[0]
    : null
  const scopedAccounts = selectedIds === null ? current.accounts : selectedIds.map((id, index) => {
    const existing = currentById.get(id)
    return normalizeAccount({
      community_id: id,
      community_name: selectedNames[index] || existing?.community_name || '',
      username: existing?.username || (selectedIds.length === 1 ? legacyAccount?.username : '') || '',
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
  if (!/^https?:\/\/[^/?#]+$/i.test(value)) throw new Error('居住证接口地址格式无效')
  return value
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
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    return await response.json()
  } finally {
    window.clearTimeout(timer)
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

function classify(payload: any): { state: 'registered' | 'not_found' | 'error'; status?: string; error?: string } {
  if (payload?.success === true && Number(payload?.code) === 200 && payload?.result && typeof payload.result === 'object') {
    const code = String(payload.result.rysfzx || '').trim()
    return { state: 'registered', status: code === '1' ? '已注销' : code === '0' ? '未注销' : '状态待核对' }
  }
  if (payload?.success === false && Number(payload?.code) === 500 && payload?.result == null && String(payload?.message || '').includes('没有查询到数据')) {
    return { state: 'not_found' }
  }
  return { state: 'error', error: authResponse(payload) ? 'authentication_expired' : 'business_error' }
}

export class OfflineResidenceClient {
  private readonly config: OfflineResidenceConfig
  private readonly tokens = new Map<string, { token: string; organizationCode: string }>()

  constructor(config: OfflineResidenceConfig) {
    this.config = { ...config, base_url: config.base_url.trim().replace(/\/+$/, '') }
  }

  private accountKey(account: OfflineResidenceAccount): string {
    return account.community_id ? `community_${account.community_id}` : `local_${account.community_code || account.username}`
  }

  private async authenticate(account: OfflineResidenceAccount, mac: string): Promise<{ token: string; organizationCode: string }> {
    const base = baseUrl(this.config)
    const checkKey = String(Date.now())
    await jsonRequest(this.config, `${base}${CAPTCHA_PATH_PREFIX}${checkKey}`, { method: 'GET' })
    const payload = await jsonRequest(this.config, `${base}${LOGIN_PATH}`, {
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
    })
    if (!payload?.success || !payload?.result?.token) throw new Error('居住证平台登录失败，请检查配置')
    const organizationCode = String(payload.result.orgCode || payload.result.org_code || account.community_code).trim()
    if (account.community_code && organizationCode && organizationCode.toUpperCase() !== account.community_code) {
      throw new Error('居住证账号返回的组织代码与所选社区不一致')
    }
    const session = { token: String(payload.result.token), organizationCode }
    return session
  }

  private async login(account: OfflineResidenceAccount): Promise<{ token: string; organizationCode: string }> {
    const session = await this.authenticate(account, await readMacAddress(this.config))
    this.tokens.set(this.accountKey(account), session)
    return session
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
      try {
        await this.authenticate(account, mac)
        allowed = true
      } catch {
        allowed = false
      }
      const result = {
        community_id: account.community_id,
        community_name: account.community_name || account.community_code || account.username,
        allowed,
      }
      results.push(result)
      onProgress?.(results.length, accounts.length, result)
      if (results.length < accounts.length) await new Promise(resolve => window.setTimeout(resolve, 250))
    }
    return results
  }

  private async lookupWithToken(identity: string, session: { token: string; organizationCode: string }): Promise<ReturnType<typeof classify>> {
    const headers = {
      'X-Access-Token': session.token,
      tenant_id: '0',
      'Content-Type': 'application/json;charset=UTF-8',
    }
    const body = JSON.stringify({ sfzh: identity, xzqh: session.organizationCode.slice(0, 6) })
    const resident = await jsonRequest(this.config, `${baseUrl(this.config)}${SEARCH_RESIDENT_PATH}`, { method: 'POST', headers, body })
    if (authResponse(resident)) return { state: 'error', error: 'authentication_expired' }
    const floating = await jsonRequest(this.config, `${baseUrl(this.config)}${SEARCH_FLOATING_PATH}`, { method: 'POST', headers, body })
    return classify(floating)
  }

  async lookup(identity: string): Promise<OfflineResidenceQueryResult> {
    if (!this.config.enabled) return { status: '查询未开启', error: 'disabled' }
    if (!this.config.password || !this.config.base_url || !this.config.accounts.length || this.config.accounts.some(account => !account.username)) {
      return { status: '配置不完整', error: 'config_incomplete' }
    }
    let lastError = ''
    let sawNotFound = false
    for (const account of this.config.accounts) {
      try {
        const key = this.accountKey(account)
        let session = this.tokens.get(key) || await this.login(account)
        let result = await this.lookupWithToken(identity, session)
        if (result.error === 'authentication_expired') {
          this.tokens.delete(key)
          session = await this.login(account)
          result = await this.lookupWithToken(identity, session)
        }
        if (result.state === 'registered') return { status: result.status || '状态待核对' }
        if (result.state === 'not_found') sawNotFound = true
        else lastError = result.error || 'business_error'
      } catch (error) {
        lastError = error instanceof Error ? error.message : 'request_error'
      }
    }
    if (sawNotFound && !lastError) return { status: '未登记' }
    return { status: '查询失败', error: lastError || 'not_found' }
  }
}
