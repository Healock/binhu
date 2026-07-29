import assert from 'node:assert/strict'
import test from 'node:test'
import {
  normalizeThemeMode,
  readStoredThemeMode,
  resolveThemeMode,
} from '../src/utils/themeMode.ts'

test('旧账号和异常主题值保持浅色', () => {
  assert.equal(normalizeThemeMode(undefined), 'light')
  assert.equal(normalizeThemeMode('unknown'), 'light')
  assert.equal(
    readStoredThemeMode({ getItem: () => null }),
    'light',
  )
})

test('浅色和深色模式不受系统设置影响', () => {
  assert.equal(resolveThemeMode('light', true), 'light')
  assert.equal(resolveThemeMode('dark', false), 'dark')
})

test('跟随系统模式随系统深浅色切换', () => {
  assert.equal(resolveThemeMode('system', false), 'light')
  assert.equal(resolveThemeMode('system', true), 'dark')
})

test('本地缓存只接受三种受支持的主题', () => {
  assert.equal(
    readStoredThemeMode({ getItem: () => 'system' }),
    'system',
  )
  assert.equal(
    readStoredThemeMode({ getItem: () => 'dark' }),
    'dark',
  )
  assert.equal(
    readStoredThemeMode({ getItem: () => 'sepia' }),
    'light',
  )
})
