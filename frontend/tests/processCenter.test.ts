import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const page = readFileSync(new URL('../src/pages/ProcessCenter.tsx', import.meta.url), 'utf8')

test('不可上传的附件字段不会注册必填表单校验', () => {
  assert.match(page, /if \(field\.type === 'attachment'\) \{[\s\S]*?return <Alert/)
  assert.match(page, /当前不会收集附件；该字段不会阻止提交。/)
  assert.match(page, /selectedDefinition\?\.form_schema\?\.fields\?\.map\(\(field: any\) => renderFormField\(field, members\)\)/)
  assert.doesNotMatch(page, /field\.type === 'attachment' \? <Alert/)
})

test('无可发起流程时入口禁用，且表单使用当前发布版本', () => {
  assert.match(page, /disabled=\{loading \|\| definitions\.length === 0\}/)
  assert.match(page, /full\.versions\?\.find\(\(version: any\) => Number\(version\.id\) === Number\(full\.current_version_id\)\)/)
  assert.match(page, /selectedDefinition\?\.form_schema\?\.fields/)
})
