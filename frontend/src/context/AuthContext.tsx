import { createContext, useContext, useState, useEffect, ReactNode } from 'react'
import {
  changeOwnPassword,
  getCurrentUser,
  recordSessionActivity,
  saveUserPreferences,
} from '../api/client'
import type { User, UserPreferences } from '../types'

interface AuthContextValue {
  user: User | null
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
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    getCurrentUser()
      .then(setUser)
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  const login = async (username: string, password: string) => {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ username, password }),
    })
    if (!res.ok) {
      const data = await res.json().catch(() => ({}))
      const detail = data.detail
      throw new Error(
        typeof detail === 'object' ? detail.message : detail || '登录失败',
      )
    }
    await res.json()
    setUser(await getCurrentUser())
  }

  const logout = async () => {
    await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).catch(() => {})
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
