import type { ThemeMode } from '../types'

export type ResolvedThemeMode = Exclude<ThemeMode, 'system'>

export const THEME_STORAGE_KEY = 'binhu-theme-mode'
export const THEME_MEDIA_QUERY = '(prefers-color-scheme: dark)'

export function normalizeThemeMode(value: unknown): ThemeMode {
  return value === 'dark' || value === 'system' ? value : 'light'
}

export function resolveThemeMode(
  mode: ThemeMode,
  systemPrefersDark: boolean,
): ResolvedThemeMode {
  if (mode === 'system') return systemPrefersDark ? 'dark' : 'light'
  return mode
}

export function readStoredThemeMode(
  storage: Pick<Storage, 'getItem'>,
): ThemeMode {
  return normalizeThemeMode(storage.getItem(THEME_STORAGE_KEY))
}

export function applyThemeToDocument(mode: ResolvedThemeMode) {
  document.documentElement.dataset.theme = mode
  document.documentElement.style.colorScheme = mode
}
