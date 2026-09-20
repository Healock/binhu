export interface OfflineResidenceAccount {
  community_id: number | null
  community_name: string
  username: string
  community_code: string
}

export interface OfflineResidenceConfig {
  enabled: boolean
  base_url: string
  username: string
  password: string
  mac_service_url: string
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

const SEARCH_RESIDENT_PATH = '/szjzz/searchIsck'
const SEARCH_FLOATING_PATH = '/szjzz/searchzzrk'
const LOGIN_PATH = '/sys/login'
const CAPTCHA_PATH_PREFIX = '/sys/randomImage/'
const STORAGE_KEY = 'binhu_offline_residence_config_v1'

export const DEFAULT_OFFLINE_RESIDENCE_CONFIG: OfflineResidenceConfig = {
  enabled: true,
  base_url: '',
  username: '',
  password: '',
  mac_service_url: 'http://127.0.0.1:23333',
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
    return {
      ...DEFAULT_OFFLINE_RESIDENCE_CONFIG,
      ...parsed,
      username: typeof parsed.username === 'string' ? parsed.username.trim() : '',
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
  localStorage.setItem(STORAGE_KEY, JSON.stringify({
    ...config,
    base_url: config.base_url.trim().replace(/\/+$/, ''),
    mac_service_url: config.mac_service_url.trim().replace(/\/+$/, ''),
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
    || typeof snapshot.mac_service_url !== 'string' || !snapshot.mac_service_url.trim()
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
    ...(typeof snapshot.mac_service_url === 'string' ? { mac_service_url: snapshot.mac_service_url } : {}),
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

  private async login(account: OfflineResidenceAccount): Promise<{ token: string; organizationCode: string }> {
    const base = baseUrl(this.config)
    const checkKey = String(Date.now())
    await jsonRequest(this.config, `${base}${CAPTCHA_PATH_PREFIX}${checkKey}`, { method: 'GET' })
    const macPayload = await jsonRequest(this.config, this.config.mac_service_url, { method: 'GET' })
    const mac = String(macPayload?.mac || '').trim()
    if (!mac) throw new Error('MAC 服务未返回设备地址')
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
    this.tokens.set(this.accountKey(account), session)
    return session
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
