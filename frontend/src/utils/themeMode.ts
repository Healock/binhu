import type { ThemeMode } from '../types'

export type ResolvedThemeMode = Exclude<ThemeMode, 'system'>

export const THEME_STORAGE_KEY = 'binhu-theme-mode'
export const THEME_MEDIA_QUERY = '(prefers-color-scheme: dark)'

const fallbackStorage = new Map<string, string>()
const inMemoryStorage: Storage = {
  get length() { return fallbackStorage.size },
  clear() { fallbackStorage.clear() },
  getItem(key) { return fallbackStorage.has(key) ? fallbackStorage.get(key)! : null },
  key(index) { return Array.from(fallbackStorage.keys())[index] ?? null },
  removeItem(key) { fallbackStorage.delete(key) },
  setItem(key, value) { fallbackStorage.set(String(key), String(value)) },
}

export function getSafeLocalStorage(): Storage {
  try {
    const storage = window.localStorage
    const probe = '__binhu_storage_probe__'
    storage.setItem(probe, '1')
    storage.removeItem(probe)
    return storage
  } catch (_error) {
    return inMemoryStorage
  }
}

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
  try {
    return normalizeThemeMode(storage.getItem(THEME_STORAGE_KEY))
  } catch (_error) {
    return 'light'
  }
}

export function systemPrefersDark(): boolean {
  try {
    return typeof window.matchMedia === 'function'
      && window.matchMedia(THEME_MEDIA_QUERY).matches
  } catch (_error) {
    return false
  }
}

export function applyThemeToDocument(mode: ResolvedThemeMode) {
  document.documentElement.dataset.theme = mode
  document.documentElement.style.colorScheme = mode
}
