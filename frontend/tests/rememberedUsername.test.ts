import assert from 'node:assert/strict'
import test from 'node:test'
import {
  clearRememberedUsername,
  MAX_REMEMBERED_USERNAMES,
  readRememberedUsername,
  readRememberedUsernames,
  REMEMBERED_USERNAME_STORAGE_KEY,
  REMEMBERED_USERNAMES_STORAGE_KEY,
  storeRememberedUsername,
} from '../src/utils/rememberedUsername.ts'

class MemoryStorage {
  values = new Map<string, string>()

  getItem(key: string) {
    return this.values.get(key) ?? null
  }

  setItem(key: string, value: string) {
    this.values.set(key, value)
  }

  removeItem(key: string) {
    this.values.delete(key)
  }
}

test('记住账号保存最近使用账号并兼容旧单值', () => {
  const storage = new MemoryStorage()

  storage.setItem(REMEMBERED_USERNAME_STORAGE_KEY, 'old-account')
  assert.deepEqual(readRememberedUsernames(storage), ['old-account'])

  storeRememberedUsername(storage, '  grid-member  ')

  assert.equal(
    storage.getItem(REMEMBERED_USERNAME_STORAGE_KEY),
    'grid-member',
  )
  assert.equal(readRememberedUsername(storage), 'grid-member')
  assert.deepEqual(readRememberedUsernames(storage), ['grid-member', 'old-account'])
  assert.deepEqual(
    JSON.parse(storage.getItem(REMEMBERED_USERNAMES_STORAGE_KEY) || '[]'),
    ['grid-member', 'old-account'],
  )
})

test('历史账号去重、最近使用置顶并限制数量', () => {
  const storage = new MemoryStorage()
  for (let index = 0; index < MAX_REMEMBERED_USERNAMES + 3; index += 1) {
    storeRememberedUsername(storage, `account-${index}`)
  }
  storeRememberedUsername(storage, 'account-5')

  const usernames = readRememberedUsernames(storage)
  assert.equal(usernames.length, MAX_REMEMBERED_USERNAMES)
  assert.equal(usernames[0], 'account-5')
  assert.equal(new Set(usernames).size, usernames.length)
})

test('历史账号列表损坏时回退到旧单值', () => {
  const storage = new MemoryStorage()
  storage.setItem(REMEMBERED_USERNAMES_STORAGE_KEY, 'not-json')
  storage.setItem(REMEMBERED_USERNAME_STORAGE_KEY, 'legacy-account')

  assert.deepEqual(readRememberedUsernames(storage), ['legacy-account'])
})

test('取消记住或用户名为空时清除本地账号', () => {
  const storage = new MemoryStorage()
  storage.setItem(REMEMBERED_USERNAME_STORAGE_KEY, 'old-account')

  storeRememberedUsername(storage, '   ')
  assert.equal(readRememberedUsername(storage), '')
  assert.deepEqual(readRememberedUsernames(storage), [])

  storage.setItem(REMEMBERED_USERNAME_STORAGE_KEY, 'another-account')
  storage.setItem(REMEMBERED_USERNAMES_STORAGE_KEY, '["another-account"]')
  clearRememberedUsername(storage)
  assert.equal(readRememberedUsername(storage), '')
  assert.equal(storage.getItem(REMEMBERED_USERNAMES_STORAGE_KEY), null)
})

test('本地存储不可用时不影响登录页读取和写入', () => {
  const unavailableStorage = {
    getItem() {
      throw new Error('storage unavailable')
    },
    setItem() {
      throw new Error('storage unavailable')
    },
    removeItem() {
      throw new Error('storage unavailable')
    },
  }

  assert.equal(readRememberedUsername(unavailableStorage), '')
  assert.deepEqual(readRememberedUsernames(unavailableStorage), [])
  assert.doesNotThrow(() => storeRememberedUsername(unavailableStorage, 'account'))
  assert.doesNotThrow(() => clearRememberedUsername(unavailableStorage))
})
