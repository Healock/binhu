import assert from 'node:assert/strict'
import test from 'node:test'
import {
  clearRememberedUsername,
  readRememberedUsername,
  REMEMBERED_USERNAME_STORAGE_KEY,
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

test('记住账号只保存规范化后的用户名', () => {
  const storage = new MemoryStorage()

  storeRememberedUsername(storage, '  grid-member  ')

  assert.equal(
    storage.getItem(REMEMBERED_USERNAME_STORAGE_KEY),
    'grid-member',
  )
  assert.equal(readRememberedUsername(storage), 'grid-member')
  assert.deepEqual([...storage.values.keys()], [REMEMBERED_USERNAME_STORAGE_KEY])
})

test('取消记住或用户名为空时清除本地账号', () => {
  const storage = new MemoryStorage()
  storage.setItem(REMEMBERED_USERNAME_STORAGE_KEY, 'old-account')

  storeRememberedUsername(storage, '   ')
  assert.equal(readRememberedUsername(storage), '')

  storage.setItem(REMEMBERED_USERNAME_STORAGE_KEY, 'another-account')
  clearRememberedUsername(storage)
  assert.equal(readRememberedUsername(storage), '')
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
  assert.doesNotThrow(() => storeRememberedUsername(unavailableStorage, 'account'))
  assert.doesNotThrow(() => clearRememberedUsername(unavailableStorage))
})
