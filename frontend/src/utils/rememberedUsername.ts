export const REMEMBERED_USERNAME_STORAGE_KEY = 'binhu_remembered_username'
export const REMEMBERED_USERNAMES_STORAGE_KEY = 'binhu_remembered_usernames'
export const MAX_REMEMBERED_USERNAMES = 8

type ReadableStorage = Pick<Storage, 'getItem'>
type WritableStorage = Pick<Storage, 'setItem' | 'removeItem'>

export function readRememberedUsername(storage: ReadableStorage): string {
  return readRememberedUsernames(storage)[0] || ''
}

function normalizeUsernames(values: unknown[]): string[] {
  const result: string[] = []
  for (const value of values) {
    if (typeof value !== 'string') continue
    const username = value.trim()
    if (!username || result.includes(username)) continue
    result.push(username)
    if (result.length >= MAX_REMEMBERED_USERNAMES) break
  }
  return result
}

export function readRememberedUsernames(storage: ReadableStorage): string[] {
  try {
    const serialized = storage.getItem(REMEMBERED_USERNAMES_STORAGE_KEY)
    if (serialized) {
      try {
        const parsed: unknown = JSON.parse(serialized)
        if (Array.isArray(parsed)) {
          const usernames = normalizeUsernames(parsed)
          if (usernames.length) return usernames
        }
      } catch {
        // Fall back to the legacy single-value key when the list is damaged.
      }
    }
    const legacyUsername = storage.getItem(REMEMBERED_USERNAME_STORAGE_KEY)?.trim() || ''
    return legacyUsername ? [legacyUsername] : []
  } catch {
    return []
  }
}

export function storeRememberedUsername(
  storage: WritableStorage,
  username: string,
): void {
  try {
    const normalizedUsername = username.trim()
    if (normalizedUsername) {
      const usernames = normalizeUsernames([
        normalizedUsername,
        ...readRememberedUsernames(storage),
      ])
      storage.setItem(REMEMBERED_USERNAME_STORAGE_KEY, normalizedUsername)
      storage.setItem(REMEMBERED_USERNAMES_STORAGE_KEY, JSON.stringify(usernames))
    } else {
      clearRememberedUsername(storage)
    }
  } catch {
    // Local storage may be unavailable in restricted browser or desktop contexts.
  }
}

export function clearRememberedUsername(storage: WritableStorage): void {
  try {
    storage.removeItem(REMEMBERED_USERNAME_STORAGE_KEY)
    storage.removeItem(REMEMBERED_USERNAMES_STORAGE_KEY)
  } catch {
    // Remembering the username must never block authentication.
  }
}
