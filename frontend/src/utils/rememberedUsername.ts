export const REMEMBERED_USERNAME_STORAGE_KEY = 'binhu_remembered_username'

type ReadableStorage = Pick<Storage, 'getItem'>
type WritableStorage = Pick<Storage, 'setItem' | 'removeItem'>

export function readRememberedUsername(storage: ReadableStorage): string {
  try {
    return storage.getItem(REMEMBERED_USERNAME_STORAGE_KEY)?.trim() || ''
  } catch {
    return ''
  }
}

export function storeRememberedUsername(
  storage: WritableStorage,
  username: string,
): void {
  try {
    const normalizedUsername = username.trim()
    if (normalizedUsername) {
      storage.setItem(REMEMBERED_USERNAME_STORAGE_KEY, normalizedUsername)
    } else {
      storage.removeItem(REMEMBERED_USERNAME_STORAGE_KEY)
    }
  } catch {
    // Local storage may be unavailable in restricted browser or desktop contexts.
  }
}

export function clearRememberedUsername(storage: WritableStorage): void {
  try {
    storage.removeItem(REMEMBERED_USERNAME_STORAGE_KEY)
  } catch {
    // Remembering the username must never block authentication.
  }
}
