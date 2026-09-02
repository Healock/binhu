import { createContext, useContext, useState, useEffect, ReactNode } from 'react'
import {
  changeOwnPassword,
  getAppBootstrap,
  getCurrentUser,
  fetchWithAuth,
  recordSessionActivity,
  saveUserPreferences,
} from '../api/client'
import type { User, UserPreferences } from '../types'
import { clearRoleDashboardCaches } from '../utils/dashboardCache'
import { detectClientDeviceType, getDeviceId } from '../utils/device.ts'
import {
  assertApiEnvironmentIdentity,
  environmentForUsername,
  getApiEnvironment,
  resetApiEnvironment,
  setApiEnvironment,
  type AppEnvironment,
} from '../utils/apiEnvironment.ts'

interface AuthContextValue {
  user: User | null
  clientVersion: string
  serverVersion: string
  systemTimezone: string
  environment: AppEnvironment
  environmentLabel: string
  loadTestRunId: string
  loading: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  updatePreferences: (preferences: UserPreferences) => Promise<void>
  refreshUser: () => Promise<void>
  recordActivity: () => Promise<void>
  changePassword: (currentPassword: string, newPassword: string) => Promise<void>
  setSystemTimezone: (timezone: string) => void
}

const AuthContext = createContext<AuthContextValue>({
  user: null,
  clientVersion: typeof __APP_VERSION__ === 'string' ? __APP_VERSION__ : '0.0.0',
  serverVersion: typeof __APP_VERSION__ === 'string' ? __APP_VERSION__ : '0.0.0',
  systemTimezone: 'Asia/Shanghai',
  environment: 'production',
  environmentLabel: '正式环境',
  loadTestRunId: '',
  loading: true,
  login: async () => {},
  logout: async () => {},
  updatePreferences: async () => {},
  refreshUser: async () => {},
  recordActivity: async () => {},
  changePassword: async () => {},
  setSystemTimezone: () => {},
})

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const clientVersion = typeof __APP_VERSION__ === 'string' ? __APP_VERSION__ : '0.0.0'
  const [serverVersion, setServerVersion] = useState(
    clientVersion,
  )
  const [systemTimezone, setSystemTimezone] = useState('Asia/Shanghai')
  const [loading, setLoading] = useState(true)
  const [environment, setEnvironment] = useState<AppEnvironment>(() => getApiEnvironment())
  const [environmentLabel, setEnvironmentLabel] = useState('正式环境')
  const [loadTestRunId, setLoadTestRunId] = useState('')

  const applyBootstrap = (payload: Awaited<ReturnType<typeof getAppBootstrap>>, expected: AppEnvironment) => {
    assertApiEnvironmentIdentity(payload.environment, expected)
    if (payload.server_version) setServerVersion(payload.server_version)
    if (payload.timezone) setSystemTimezone(payload.timezone)
    setEnvironment(expected)
    setEnvironmentLabel(payload.environment_label || (expected === 'shadow' ? '影子压测环境' : '正式环境'))
    setLoadTestRunId(payload.load_test_run_id || '')
  }

  useEffect(() => {
    const expected = getApiEnvironment()
    const restoreSession = async () => {
      try {
        const payload = await getAppBootstrap()
        applyBootstrap(payload, expected)
        setUser(await getCurrentUser())
      } catch (error) {
        setUser(null)
        if (expected === 'shadow') {
          const message = error instanceof Error ? error.message : '影子压测环境当前不可用'
          sessionStorage.setItem('auth_exit_reason', JSON.stringify({
            code: 'shadow_environment_unavailable',
            message,
          }))
        }
      } finally {
        setLoading(false)
      }
    }
    void restoreSession()
  }, [])

  const login = async (username: string, password: string) => {
    const targetEnvironment = environmentForUsername(username)
    setApiEnvironment(targetEnvironment)
    setEnvironment(targetEnvironment)
    try {
      const bootstrap = await getAppBootstrap()
      applyBootstrap(bootstrap, targetEnvironment)
    } catch (error) {
      if (targetEnvironment === 'shadow') {
        throw new Error(
          error instanceof Error && /非影子服务/.test(error.message)
            ? error.message
            : '影子压测环境当前未开启，请联系管理员启动后重试',
        )
      }
      throw error
    }
    const res = await fetchWithAuth('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({
        username,
        password,
        device_type: detectClientDeviceType(),
        device_id: getDeviceId(),
      }),
    }, { handleUnauthorized: false, markActivity: false })
    if (!res.ok) {
      const data = await res.json().catch(() => ({}))
      const detail = data.detail
      throw new Error(
        typeof detail === 'object' ? detail.message : detail || '登录失败',
      )
    }
    await res.json()
    clearRoleDashboardCaches(window.sessionStorage)
    setUser(await getCurrentUser())
  }

  const logout = async () => {
    await fetchWithAuth(
      '/api/auth/logout',
      { method: 'POST' },
      { handleUnauthorized: false, markActivity: false },
    ).catch(() => {})
    clearRoleDashboardCaches(window.sessionStorage)
    setUser(null)
    resetApiEnvironment()
    setEnvironment('production')
    setEnvironmentLabel('正式环境')
    setLoadTestRunId('')
  }

  const updatePreferences = async (preferences: UserPreferences) => {
    const updatedUser = await saveUserPreferences(preferences)
    setUser(updatedUser)
  }

  const refreshUser = async () => {
    setUser(await getCurrentUser())
  }

  const recordActivity = async () => {
    setUser(await recordSessionActivity())
  }

  const changePassword = async (currentPassword: string, newPassword: string) => {
    await changeOwnPassword(currentPassword, newPassword)
    await refreshUser()
  }

  return (
    <AuthContext.Provider value={{
      user,
      clientVersion,
      serverVersion,
      systemTimezone,
      environment,
      environmentLabel,
      loadTestRunId,
      loading,
      login,
      logout,
      updatePreferences,
      refreshUser,
      recordActivity,
      changePassword,
      setSystemTimezone,
    }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  return useContext(AuthContext)
}
