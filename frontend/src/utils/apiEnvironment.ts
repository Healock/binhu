export type AppEnvironment = 'production' | 'shadow'

export interface ApiEnvironmentSnapshot {
  environment: AppEnvironment
  apiBaseUrl: string
}

const STORAGE_KEY = 'binhu_api_environment'
const SHADOW_SUFFIX = '@shadow'

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
  return username.trim().toLowerCase().endsWith(SHADOW_SUFFIX) ? 'shadow' : 'production'
}

export function getApiEnvironment(): AppEnvironment {
  return safeSessionStorage()?.getItem(STORAGE_KEY) === 'shadow' ? 'shadow' : 'production'
}

export function setApiEnvironment(environment: AppEnvironment): void {
  const storage = safeSessionStorage()
  if (storage) {
    if (environment === 'shadow') storage.setItem(STORAGE_KEY, environment)
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
  if (/^https?:\/\//i.test(productionBase)) {
    return `${new URL(productionBase).origin}/shadow-api`
  }
  return '/shadow-api'
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
  throw new Error(
    expected === 'shadow'
      ? '影子入口连接到了非影子服务，已阻止登录'
      : '正式入口环境身份校验失败，已阻止登录',
  )
}

export function resolveRuntimeApiUrl(input: string): string {
  if (/^[a-z][a-z\d+.-]*:/i.test(input) || input.startsWith('//')) {
    if (getApiEnvironment() !== 'shadow') return input
    const browserOrigin = typeof window !== 'undefined' ? window.location.origin : 'http://localhost'
    const target = new URL(input, browserOrigin)
    const shadowBase = new URL(getApiBaseUrl('shadow'), browserOrigin)
    if (target.origin !== shadowBase.origin) {
      throw new Error('影子环境已阻止跨域 API 请求')
    }
    if (target.pathname === '/api' || target.pathname.startsWith('/api/')) {
      target.pathname = `/shadow-api${target.pathname.slice(4)}`
    } else if (target.pathname !== '/shadow-api' && !target.pathname.startsWith('/shadow-api/')) {
      throw new Error('影子环境只允许访问固定的 /shadow-api 入口')
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
    if (getApiEnvironment() !== 'shadow') return assetUrl
    try {
      const target = new URL(assetUrl, window.location.origin)
      const apiBase = new URL(getApiBaseUrl(), window.location.origin)
      if (target.origin !== apiBase.origin) return null
      if (target.pathname === '/api' || target.pathname.startsWith('/api/')) {
        return resolveRuntimeApiUrl(target.toString())
      }
      return target.pathname === '/shadow-api' || target.pathname.startsWith('/shadow-api/')
        ? target.toString()
        : null
    } catch {
      return null
    }
  }
  return resolveRuntimeApiUrl(assetUrl)
}
