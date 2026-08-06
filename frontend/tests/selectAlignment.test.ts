import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const stylesheet = readFileSync(
  new URL('../src/index.css', import.meta.url),
  'utf8',
)

test('触控高度只作用于业务表单且不再全局移动下拉文字', () => {
  assert.match(
    stylesheet,
    /\.mobile-task-page \.ant-select-single,[\s\S]*--ant-select-height:\s*44px;/,
  )
  assert.doesNotMatch(stylesheet, /\.ant-select-content-value,[\s\S]{0,300}translateY\(1px\)/)
  assert.match(stylesheet, /input:not\(\[type='checkbox'\]\)[\s\S]*:not\(\[class\^='ant-'\]\)/)
})
