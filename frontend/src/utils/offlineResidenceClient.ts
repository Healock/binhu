export interface OfflineResidenceConfig {
  enabled: boolean
  base_url: string
  username: string
  password: string
  mac_service_url: string
  timeout_seconds: number
  community_codes: string[]
}

export interface OnlineResidenceConfigSnapshot {
  enabled?: boolean
  base_url?: string
  mac_service_url?: string
  timeout_seconds?: number
  community_codes?: string[]
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
}

export function loadOfflineResidenceConfig(): OfflineResidenceConfig {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return { ...DEFAULT_OFFLINE_RESIDENCE_CONFIG }
    const parsed = JSON.parse(raw) as Partial<OfflineResidenceConfig>
    return {
      ...DEFAULT_OFFLINE_RESIDENCE_CONFIG,
      ...parsed,
      username: typeof parsed.username === 'string' ? parsed.username.trim() : '',
      community_codes: Array.isArray(parsed.community_codes) ? parsed.community_codes.map(String).filter(Boolean) : [],
      timeout_seconds: Math.min(120, Math.max(1, Number(parsed.timeout_seconds || 15))),
    }
  } catch {
    return { ...DEFAULT_OFFLINE_RESIDENCE_CONFIG }
  }
}

export function saveOfflineResidenceConfig(config: OfflineResidenceConfig): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({
    ...config,
    base_url: config.base_url.trim().replace(/\/+$/, ''),
    username: config.username.trim(),
    mac_service_url: config.mac_service_url.trim().replace(/\/+$/, ''),
    community_codes: config.community_codes.map(item => item.trim().toUpperCase()).filter(Boolean),
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
  const next: OfflineResidenceConfig = {
    ...current,
    ...(typeof snapshot.enabled === 'boolean' ? { enabled: snapshot.enabled } : {}),
    ...(typeof snapshot.base_url === 'string' ? { base_url: snapshot.base_url } : {}),
    ...(typeof snapshot.mac_service_url === 'string' ? { mac_service_url: snapshot.mac_service_url } : {}),
    ...(typeof snapshot.timeout_seconds === 'number' ? { timeout_seconds: snapshot.timeout_seconds } : {}),
    ...(Array.isArray(snapshot.community_codes) ? { community_codes: snapshot.community_codes } : {}),
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

  private async login(communityCode: string): Promise<{ token: string; organizationCode: string }> {
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
        username: this.config.username,
        password: this.config.password,
        mac,
        remember_me: true,
        captcha: '',
        checkKey,
        terminalType: 1,
      }),
    })
    if (!payload?.success || !payload?.result?.token) throw new Error('居住证平台登录失败，请检查配置')
    const organizationCode = String(payload.result.orgCode || payload.result.org_code || communityCode).trim()
    const session = { token: String(payload.result.token), organizationCode }
    this.tokens.set(communityCode, session)
    return session
  }

  private async lookupWithToken(identity: string, communityCode: string, session: { token: string; organizationCode: string }): Promise<ReturnType<typeof classify>> {
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
    if (!this.config.password || !this.config.username || !this.config.base_url) return { status: '配置不完整', error: 'config_incomplete' }
    const codes = this.config.community_codes.map(value => value.trim().toUpperCase()).filter(Boolean)
    // The full account is entered locally. Community codes remain an optional
    // organization fallback for older installations and never derive credentials.
    const lookupCodes = codes.length ? codes : ['']
    let lastError = ''
    let sawNotFound = false
    for (const code of lookupCodes) {
      try {
        let session = this.tokens.get(code) || await this.login(code)
        let result = await this.lookupWithToken(identity, code, session)
        if (result.error === 'authentication_expired') {
          this.tokens.delete(code)
          session = await this.login(code)
          result = await this.lookupWithToken(identity, code, session)
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
