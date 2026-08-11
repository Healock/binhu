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

interface AuthContextValue {
  user: User | null
  serverVersion: string
  loading: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  updatePreferences: (preferences: UserPreferences) => Promise<void>
  refreshUser: () => Promise<void>
  recordActivity: () => Promise<void>
  changePassword: (currentPassword: string, newPassword: string) => Promise<void>
}

const AuthContext = createContext<AuthContextValue>({
  user: null,
  serverVersion: typeof __APP_VERSION__ === 'string' ? __APP_VERSION__ : '0.0.0',
  loading: true,
  login: async () => {},
  logout: async () => {},
  updatePreferences: async () => {},
  refreshUser: async () => {},
  recordActivity: async () => {},
  changePassword: async () => {},
})

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [serverVersion, setServerVersion] = useState(
    typeof __APP_VERSION__ === 'string' ? __APP_VERSION__ : '0.0.0',
  )
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    getAppBootstrap()
      .then(payload => {
        if (payload.server_version) setServerVersion(payload.server_version)
      })
      .catch(() => {})
    getCurrentUser()
      .then(setUser)
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  const login = async (username: string, password: string) => {
    const res = await fetchWithAuth('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ username, password }),
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
      serverVersion,
      loading,
      login,
      logout,
      updatePreferences,
      refreshUser,
      recordActivity,
      changePassword,
    }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  return useContext(AuthContext)
}
