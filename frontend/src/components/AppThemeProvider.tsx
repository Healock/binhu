import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { ConfigProvider, theme as antdTheme, type ThemeConfig } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { useAuth } from '../context/AuthContext'
import {
  applyThemeToDocument,
  normalizeThemeMode,
  readStoredThemeMode,
  resolveThemeMode,
  THEME_MEDIA_QUERY,
  THEME_STORAGE_KEY,
} from '../utils/themeMode'

const lightTheme: ThemeConfig = {
  algorithm: antdTheme.defaultAlgorithm,
  token: {
    colorPrimary: '#1d4ed8',
    colorSuccess: '#16a34a',
    colorSuccessBg: '#f0fdf4',
    colorSuccessBgHover: '#dcfce7',
    colorSuccessBorder: '#bbf7d0',
    colorSuccessBorderHover: '#86efac',
    colorSuccessText: '#166534',
    colorSuccessTextHover: '#15803d',
    colorSuccessTextActive: '#14532d',
    colorWarning: '#c2410c',
    colorError: '#b91c1c',
    colorText: '#172033',
    colorTextSecondary: '#64748b',
    colorBorder: '#dbe2ea',
    colorBorderSecondary: '#e2e8f0',
    colorBgLayout: '#f4f6f9',
    colorBgContainer: '#ffffff',
    colorBgElevated: '#ffffff',
    borderRadius: 8,
    controlHeight: 36,
    fontSize: 14,
    boxShadowSecondary: '0 8px 24px rgba(15, 23, 42, 0.10)',
  },
  components: {
    Button: {
      fontWeight: 500,
      primaryShadow: 'none',
    },
    Table: {
      headerBg: '#f8fafc',
      headerColor: '#475569',
      borderColor: '#e2e8f0',
      rowHoverBg: '#f8fbff',
    },
    Menu: {
      itemBorderRadius: 8,
      itemSelectedBg: '#eff6ff',
      itemSelectedColor: '#1d4ed8',
    },
  },
}

const darkTheme: ThemeConfig = {
  algorithm: antdTheme.darkAlgorithm,
  token: {
    colorPrimary: '#60a5fa',
    colorSuccess: '#34d399',
    colorSuccessBg: '#0d2b22',
    colorSuccessBgHover: '#12382b',
    colorSuccessBorder: '#276749',
    colorSuccessBorderHover: '#3a8a65',
    colorSuccessText: '#a7f3d0',
    colorSuccessTextHover: '#bbf7d0',
    colorSuccessTextActive: '#6ee7b7',
    colorWarning: '#fb923c',
    colorError: '#f87171',
    colorText: '#e5edf7',
    colorTextSecondary: '#94a3b8',
    colorBorder: '#2b3a4f',
    colorBorderSecondary: '#253247',
    colorBgBase: '#080d17',
    colorBgLayout: '#080d17',
    colorBgContainer: '#101826',
    colorBgElevated: '#162133',
    borderRadius: 8,
    controlHeight: 36,
    fontSize: 14,
    boxShadowSecondary: '0 12px 30px rgba(0, 0, 0, 0.34)',
  },
  components: {
    Button: {
      fontWeight: 500,
      primaryShadow: 'none',
    },
    Table: {
      headerBg: '#151f2e',
      headerColor: '#b8c5d8',
      borderColor: '#253247',
      rowHoverBg: '#16243a',
    },
    Menu: {
      itemBorderRadius: 8,
      itemSelectedBg: '#172d50',
      itemSelectedColor: '#93c5fd',
    },
  },
}

export default function AppThemeProvider({
  children,
}: {
  children: ReactNode
}) {
  const { user } = useAuth()
  const [systemPrefersDark, setSystemPrefersDark] = useState(() => (
    window.matchMedia(THEME_MEDIA_QUERY).matches
  ))
  const storedMode = readStoredThemeMode(window.localStorage)
  const selectedMode = normalizeThemeMode(user?.theme_mode ?? storedMode)
  const resolvedMode = resolveThemeMode(selectedMode, systemPrefersDark)

  useEffect(() => {
    const media = window.matchMedia(THEME_MEDIA_QUERY)
    const handleChange = (event: MediaQueryListEvent) => {
      setSystemPrefersDark(event.matches)
    }
    setSystemPrefersDark(media.matches)
    media.addEventListener('change', handleChange)
    return () => media.removeEventListener('change', handleChange)
  }, [])

  useEffect(() => {
    if (!user?.theme_mode) return
    window.localStorage.setItem(
      THEME_STORAGE_KEY,
      normalizeThemeMode(user.theme_mode),
    )
  }, [user?.theme_mode])

  useLayoutEffect(() => {
    applyThemeToDocument(resolvedMode)
  }, [resolvedMode])

  const themeConfig = useMemo(
    () => resolvedMode === 'dark' ? darkTheme : lightTheme,
    [resolvedMode],
  )

  return (
    <ConfigProvider locale={zhCN} theme={themeConfig}>
      {children}
    </ConfigProvider>
  )
}
