import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const stylesheet = readFileSync(
  new URL('../src/index.css', import.meta.url),
  'utf8',
)

test('单选下拉框同步移动端高度变量并校正文字基线', () => {
  assert.match(
    stylesheet,
    /\.app-shell \.ant-select-single\s*\{[^}]*--ant-select-height:\s*44px;/s,
  )
  assert.match(
    stylesheet,
    /\.ant-select-content-value,[\s\S]*transform:\s*translateY\(1px\);/,
  )
})
