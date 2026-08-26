import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const statusSource = readFileSync(
  new URL('../src/components/ResidenceRegistrationStatus.tsx', import.meta.url),
  'utf8',
)
const listSource = readFileSync(new URL('../src/pages/MobileTaskList.tsx', import.meta.url), 'utf8')
const tableSource = readFileSync(new URL('../src/components/MobileTaskTable.tsx', import.meta.url), 'utf8')
const detailSource = readFileSync(new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url), 'utf8')
const settingsSource = readFileSync(new URL('../src/pages/SystemSettings.tsx', import.meta.url), 'utf8')
const communitiesSource = readFileSync(new URL('../src/pages/Communities.tsx', import.meta.url), 'utf8')

test('only a confirmed missing residence record is highlighted as first registration', () => {
  assert.match(statusSource, /first_registration:\s*\{ label: '首次登记'/)
  assert.match(statusSource, /compact && status\.state !== 'first_registration'/)
  assert.match(statusSource, /error:\s*\{ label: '居住证查询待核对'/)
})

test('card table and detail all render the shared residence status component', () => {
  assert.match(listSource, /<ResidenceRegistrationStatus status=\{task\.residence_status\} compact/)
  assert.match(tableSource, /<ResidenceRegistrationStatus status=\{task\.residence_status\} compact/)
  assert.match(detailSource, /<ResidenceRegistrationStatus status=\{data\.task\.residence_status\}/)
})

test('the residence status tooltip shows the measured lookup duration', () => {
  assert.match(statusSource, /status\.duration_ms \/ 1000/)
  assert.match(statusSource, /本次耗时/)
})

test('system settings uses automatic community accounts without a manual captcha', () => {
  assert.match(settingsSource, /居住证平台首次登记识别/)
  assert.match(settingsSource, /只调用常住人口预检索和流动人口登记查询两个只读接口/)
  assert.match(settingsSource, /全民防社区代码并在末尾加 00/)
  assert.match(settingsSource, /统一登录密码/)
  assert.match(settingsSource, /网页本身无需人工填写验证码/)
  assert.doesNotMatch(settingsSource, /获取登录验证码|登录并开始查询/)
  assert.doesNotMatch(settingsSource, /居住证.*(?:登记提交|注销提交|保存人员)/)
  assert.match(communitiesSource, /居住证账号/)
  assert.match(communitiesSource, /qmf_community_code\}00/)
})
