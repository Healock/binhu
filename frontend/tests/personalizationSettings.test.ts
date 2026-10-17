import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const read = () => readFileSync(
  new URL('../src/pages/PersonalizationSettings.tsx', import.meta.url),
  'utf8',
)

test('会话活动刷新用户资料时不重置未保存的个性化设置', () => {
  const source = read()
  assert.match(source, /setColumnMode\(user\.report_column_mode \|\| 'three'\)/)
  assert.match(source, /setDockConfig\(normalizeMobileDockConfig\(/)
  assert.match(source, /\}, \[user\?\.id\]\)/)
  assert.doesNotMatch(source, /\}, \[user\]\)/)
})

