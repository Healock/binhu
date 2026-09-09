export type AppEnvironment = 'production' | 'staging' | 'development' | 'shadow'

export interface ApiEnvironmentSnapshot {
  environment: AppEnvironment
  apiBaseUrl: string
}

const STORAGE_KEY = 'binhu_api_environment'
const ENVIRONMENT_SUFFIXES: Array<[string, AppEnvironment]> = [
  ['@staging', 'staging'],
  ['@dev', 'development'],
  ['@shadow', 'shadow'],
]

function configuredProductionBaseUrl(): string {
  return String(import.meta.env?.VITE_API_BASE_URL || '').replace(/\/+$/, '') || '/api'
}

function safeSessionStorage(): Storage | null {
  try {
    return typeof sessionStorage !== 'undefined' ? sessionStorage : null
  } catch {
    return null
  }
}

export function environmentForUsername(username: string): AppEnvironment {
  const normalized = username.trim().toLowerCase()
  return ENVIRONMENT_SUFFIXES.find(([suffix]) => normalized.endsWith(suffix))?.[1] || 'production'
}

export function getApiEnvironment(): AppEnvironment {
  if (typeof window !== 'undefined') {
    const path = window.location.pathname.toLowerCase()
    if (path === '/staging' || path.startsWith('/staging/')) return 'staging'
    if (path === '/dev' || path.startsWith('/dev/')) return 'development'
  }
  const stored = safeSessionStorage()?.getItem(STORAGE_KEY)
  if (stored === 'staging' || stored === 'development' || stored === 'shadow') return stored
  return 'production'
}

export function setApiEnvironment(environment: AppEnvironment): void {
  const storage = safeSessionStorage()
  if (storage) {
    if (environment !== 'production') storage.setItem(STORAGE_KEY, environment)
    else storage.removeItem(STORAGE_KEY)
  }
  if (
    typeof window !== 'undefined'
    && typeof window.dispatchEvent === 'function'
    && typeof CustomEvent !== 'undefined'
  ) {
    window.dispatchEvent(new CustomEvent('binhu:api-environment-changed', { detail: environment }))
  }
}

export function resetApiEnvironment(): void {
  setApiEnvironment('production')
}

export function getApiBaseUrl(environment = getApiEnvironment()): string {
  const productionBase = configuredProductionBaseUrl()
  if (environment === 'production') return productionBase
  const path = environment === 'staging' ? '/staging/api' : environment === 'development' ? '/dev/api' : '/shadow-api'
  if (/^https?:\/\//i.test(productionBase)) {
    return `${new URL(productionBase).origin}${path}`
  }
  return path
}

export function getApiEnvironmentSnapshot(): ApiEnvironmentSnapshot {
  const environment = getApiEnvironment()
  return { environment, apiBaseUrl: getApiBaseUrl(environment) }
}

export function assertApiEnvironmentIdentity(
  actual: string | null | undefined,
  expected: AppEnvironment,
): void {
  if (actual === expected) return
  if (expected === 'production') {
    throw new Error('正式入口环境身份校验失败，已阻止登录')
  }
  throw new Error(
    `当前入口连接到了非${expected === 'staging' ? '预发布' : expected === 'development' ? 'Dev' : '影子'}环境服务，已阻止登录`,
  )
}

export function resolveRuntimeApiUrl(input: string): string {
  if (/^[a-z][a-z\d+.-]*:/i.test(input) || input.startsWith('//')) {
    if (getApiEnvironment() === 'production') return input
    const browserOrigin = typeof window !== 'undefined' ? window.location.origin : 'http://localhost'
    const target = new URL(input, browserOrigin)
    const environmentBase = new URL(getApiBaseUrl(), browserOrigin)
    if (target.origin !== environmentBase.origin) {
      throw new Error('当前环境已阻止跨域 API 请求')
    }
    const basePath = environmentBase.pathname.replace(/\/$/, '')
    if (target.pathname === '/api' || target.pathname.startsWith('/api/')) {
      target.pathname = `${basePath}${target.pathname.slice(4)}`
    } else if (target.pathname !== basePath && !target.pathname.startsWith(`${basePath}/`)) {
      throw new Error('当前环境只允许访问固定的 API 入口')
    }
    return target.toString()
  }
  const baseUrl = getApiBaseUrl()
  if (input === '/api') return baseUrl
  if (input.startsWith('/api/')) return `${baseUrl}${input.slice(4)}`
  return `${baseUrl}${input.startsWith('/') ? input : `/${input}`}`
}

export function resolveRuntimeAssetUrl(assetUrl: string | null | undefined): string | null {
  if (!assetUrl) return null
  if (/^[a-z][a-z\d+.-]*:/i.test(assetUrl) || assetUrl.startsWith('//')) {
    if (getApiEnvironment() === 'production') return assetUrl
    try {
      const target = new URL(assetUrl, window.location.origin)
      const apiBase = new URL(getApiBaseUrl(), window.location.origin)
      if (target.origin !== apiBase.origin) return null
      if (target.pathname === '/api' || target.pathname.startsWith('/api/')) {
        return resolveRuntimeApiUrl(target.toString())
      }
      const basePath = apiBase.pathname.replace(/\/$/, '')
      return target.pathname === basePath || target.pathname.startsWith(`${basePath}/`)
        ? target.toString()
        : null
    } catch {
      return null
    }
  }
  return resolveRuntimeApiUrl(assetUrl)
}
