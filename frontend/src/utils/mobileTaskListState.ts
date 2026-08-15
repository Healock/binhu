import type { MobileTaskFacets, MobileTaskItem } from '../api/client'

export type MobileTaskListMode = 'tasks' | 'analysis'
export type MobileTaskListDisplayMode = 'card' | 'table'

export interface MobileTaskListRestoration {
  version: 1
  mode: MobileTaskListMode
  return_url: string
  display_mode: MobileTaskListDisplayMode
  scroll_top: number
  page: number
  loaded_page: number
  keyword: string
  row_key: string
  saved_at: number
}

export interface MobileTaskListSnapshot {
  mode: MobileTaskListMode
  display_mode: MobileTaskListDisplayMode
  rows: MobileTaskItem[]
  total: number
  page: number
  loaded_page: number
  facets: MobileTaskFacets
  source_message: string
  saved_at: number
}

const STORAGE_KEY = 'mobile-task-list-restoration:v1'
const MAX_AGE_MS = 30 * 60 * 1000
let taskListSnapshot: MobileTaskListSnapshot | null = null

export function writeMobileTaskListSnapshot(snapshot: MobileTaskListSnapshot) {
  taskListSnapshot = snapshot
}

export function readMobileTaskListSnapshot(
  mode: MobileTaskListMode,
  displayMode: MobileTaskListDisplayMode,
  now = Date.now(),
): MobileTaskListSnapshot | null {
  if (
    !taskListSnapshot
    || taskListSnapshot.mode !== mode
    || taskListSnapshot.display_mode !== displayMode
    || now - taskListSnapshot.saved_at > MAX_AGE_MS
  ) return null
  return taskListSnapshot
}

export function clearMobileTaskListSnapshot() {
  taskListSnapshot = null
}

function isRestoration(value: unknown): value is MobileTaskListRestoration {
  if (!value || typeof value !== 'object') return false
  const item = value as Partial<MobileTaskListRestoration>
  return item.version === 1
    && (item.mode === 'tasks' || item.mode === 'analysis')
    && (item.display_mode === 'card' || item.display_mode === 'table')
    && typeof item.return_url === 'string'
    && typeof item.scroll_top === 'number'
    && typeof item.page === 'number'
    && typeof item.loaded_page === 'number'
    && typeof item.keyword === 'string'
    && typeof item.row_key === 'string'
    && typeof item.saved_at === 'number'
}

export function writeMobileTaskListRestoration(
  storage: Pick<Storage, 'setItem'>,
  state: MobileTaskListRestoration,
) {
  storage.setItem(STORAGE_KEY, JSON.stringify(state))
}

export function readMobileTaskListRestoration(
  storage: Pick<Storage, 'getItem' | 'removeItem'>,
  mode: MobileTaskListMode,
  displayMode: MobileTaskListDisplayMode,
  now?: number,
): MobileTaskListRestoration | null
export function readMobileTaskListRestoration(
  storage: Pick<Storage, 'getItem' | 'removeItem'>,
  mode: MobileTaskListMode,
  returnUrl: string,
  displayMode: MobileTaskListDisplayMode,
  now?: number,
): MobileTaskListRestoration | null
export function readMobileTaskListRestoration(
  storage: Pick<Storage, 'getItem' | 'removeItem'>,
  mode: MobileTaskListMode,
  returnUrlOrDisplayMode: string,
  displayModeOrNow?: MobileTaskListDisplayMode | number,
  now = Date.now(),
): MobileTaskListRestoration | null {
  const displayMode = typeof displayModeOrNow === 'string'
    ? displayModeOrNow
    : returnUrlOrDisplayMode as MobileTaskListDisplayMode
  if (typeof displayModeOrNow === 'number') now = displayModeOrNow
  try {
    const raw = storage.getItem(STORAGE_KEY)
    if (!raw) return null
    const state: unknown = JSON.parse(raw)
    if (
      !isRestoration(state)
      || state.mode !== mode
      || state.display_mode !== displayMode
      || now - state.saved_at > MAX_AGE_MS
    ) {
      storage.removeItem(STORAGE_KEY)
      return null
    }
    return {
      ...state,
      scroll_top: Math.max(0, state.scroll_top),
      page: Math.max(1, Math.floor(state.page)),
      loaded_page: Math.max(1, Math.floor(state.loaded_page)),
    }
  } catch {
    storage.removeItem(STORAGE_KEY)
    return null
  }
}

export function clearMobileTaskListRestoration(
  storage: Pick<Storage, 'removeItem'>,
) {
  storage.removeItem(STORAGE_KEY)
}
