export interface PersistedTaskDraft {
  key: string
  accountScope: string
  taskKey: string
  field: string
  value: string | null
  baseRevision: number | null
  baseValueHash: string
  draftVersion: number
  updatedAt: number
  expiresAt: number
}

const DB_NAME = 'binhu-task-drafts'
const STORE_NAME = 'drafts'
const DB_VERSION = 1
const DEFAULT_TTL_MS = 7 * 24 * 60 * 60 * 1000
const memoryStore = new Map<string, PersistedTaskDraft>()
let databasePromise: Promise<IDBDatabase | null> | null = null

export function taskDraftKey(accountScope: string, taskKey: string, field: string): string {
  return `${accountScope}:${taskKey}:${field}`
}

export function taskDraftValueHash(value: string | null): string {
  const input = value || ''
  let hash = 2166136261
  for (let index = 0; index < input.length; index += 1) {
    hash ^= input.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return (hash >>> 0).toString(16)
}

function openDatabase(): Promise<IDBDatabase | null> {
  if (databasePromise) return databasePromise
  if (typeof indexedDB === 'undefined') return Promise.resolve(null)
  databasePromise = new Promise(resolve => {
    const request = indexedDB.open(DB_NAME, DB_VERSION)
    request.onerror = () => resolve(null)
    request.onupgradeneeded = () => {
      const database = request.result
      const store = database.objectStoreNames.contains(STORE_NAME)
        ? request.transaction?.objectStore(STORE_NAME)
        : database.createObjectStore(STORE_NAME, { keyPath: 'key' })
      store?.createIndex('accountScope', 'accountScope', { unique: false })
      store?.createIndex('task', ['accountScope', 'taskKey'], { unique: false })
      store?.createIndex('expiresAt', 'expiresAt', { unique: false })
    }
    request.onsuccess = () => resolve(request.result)
  })
  return databasePromise
}

function withStore<T>(mode: IDBTransactionMode, action: (store: IDBObjectStore) => IDBRequest | void): Promise<T | undefined> {
  return openDatabase().then(database => {
    if (!database) return undefined
    return new Promise<T | undefined>(resolve => {
      const transaction = database.transaction(STORE_NAME, mode)
      const request = action(transaction.objectStore(STORE_NAME))
      transaction.oncomplete = () => resolve(request && 'result' in request ? (request.result as T) : undefined)
      transaction.onerror = () => resolve(undefined)
      transaction.onabort = () => resolve(undefined)
    })
  })
}

export async function putTaskDraft(input: Omit<PersistedTaskDraft, 'key' | 'updatedAt' | 'expiresAt'> & { ttlMs?: number }): Promise<void> {
  const now = Date.now()
  const record: PersistedTaskDraft = {
    ...input,
    key: taskDraftKey(input.accountScope, input.taskKey, input.field),
    updatedAt: now,
    expiresAt: now + (input.ttlMs || DEFAULT_TTL_MS),
  }
  memoryStore.set(record.key, record)
  await withStore('readwrite', store => store.put(record))
}

export async function getTaskDrafts(accountScope: string, taskKey: string): Promise<PersistedTaskDraft[]> {
  const now = Date.now()
  const memory = [...memoryStore.values()].filter(item => item.accountScope === accountScope && item.taskKey === taskKey && item.expiresAt > now)
  const database = await openDatabase()
  if (!database) return memory
  const result = await withStore<PersistedTaskDraft[]>('readonly', store => store.index('task').getAll([accountScope, taskKey]))
  const records = (result || []).filter(item => item.expiresAt > now)
  records.forEach(item => memoryStore.set(item.key, item))
  return records
}

export async function deleteTaskDraft(accountScope: string, taskKey: string, field: string): Promise<void> {
  const key = taskDraftKey(accountScope, taskKey, field)
  memoryStore.delete(key)
  await withStore('readwrite', store => store.delete(key))
}

export async function clearExpiredTaskDrafts(now = Date.now()): Promise<void> {
  for (const [key, record] of memoryStore) if (record.expiresAt <= now) memoryStore.delete(key)
  const database = await openDatabase()
  if (!database) return
  const records = await withStore<PersistedTaskDraft[]>('readonly', store => store.getAll())
  await Promise.all((records || []).filter(record => record.expiresAt <= now).map(record => withStore('readwrite', store => store.delete(record.key))))
}

export function taskDraftTtlMs(): number {
  return DEFAULT_TTL_MS
}
