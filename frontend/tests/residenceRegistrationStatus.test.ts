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
const registrationDetailSource = readFileSync(
  new URL('../src/components/ResidenceRegistrationDetail.tsx', import.meta.url),
  'utf8',
)
const settingsSource = readFileSync(new URL('../src/pages/SystemSettings.tsx', import.meta.url), 'utf8')
const communitiesSource = readFileSync(new URL('../src/pages/Communities.tsx', import.meta.url), 'utf8')
const apiSource = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8')
const taskQueueSource = readFileSync(new URL('../src/components/AdminTaskQueueFloat.tsx', import.meta.url), 'utf8')

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

test('registered tasks load and render the whitelisted residence person detail', () => {
  assert.match(detailSource, /data\?\.task\.residence_status\?\.state !== 'registered'/)
  assert.match(detailSource, /getMobileTaskResidenceDetail\(parserType, rowKey\)/)
  assert.match(detailSource, /<ResidenceRegistrationDetail/)
  for (const label of ['注销状态', '年龄 / 出生日期', '民族', '系统登记住址', '户籍地址']) {
    assert.match(registrationDetailSource, new RegExp(label))
  }
  assert.match(registrationDetailSource, /detail\.photo_data_url/)
  assert.match(registrationDetailSource, /暂无照片/)
  assert.match(registrationDetailSource, /md:grid-cols/)
})

test('the residence status tooltip shows the measured lookup duration', () => {
  assert.match(statusSource, /status\.duration_ms \/ 1000/)
  assert.match(statusSource, /本次耗时/)
})

test('community accounts are maintained per community and selected in system settings', () => {
  assert.match(settingsSource, /居住证平台首次登记识别/)
  assert.match(settingsSource, /login_community_ids: residenceConfig\.login_community_ids/)
  assert.doesNotMatch(settingsSource, /login_community_id: residenceConfig\.login_community_id/)
  assert.match(settingsSource, /placeholder="请选择登录社区（可多选）"/)
  assert.match(settingsSource, /community_options\.map/)
  assert.match(settingsSource, /未配置账号/)
  assert.match(settingsSource, /请先到社区管理填写该社区的居住证完整登录账号/)
  assert.doesNotMatch(settingsSource, /settings-field__label font-medium">完整登录账号/)
  assert.doesNotMatch(settingsSource, /username: residenceConfig\.username/)
  assert.match(settingsSource, /统一登录密码/)
  assert.match(settingsSource, /网页无需人工填写验证码/)
  assert.match(settingsSource, /全量查询间隔（分钟）/)
  assert.match(settingsSource, /重新查询全部流口指令核查对象/)
  assert.doesNotMatch(settingsSource, /请先保存完整配置/)
  assert.doesNotMatch(settingsSource, /获取登录验证码|登录并开始查询/)
  assert.doesNotMatch(settingsSource, /居住证.*(?:登记提交|注销提交|保存人员)/)
  assert.match(communitiesSource, /居住证完整登录账号/)
  assert.match(communitiesSource, /residenceUsernameDraft/)
  assert.match(communitiesSource, /已配置；留空保持不变/)
  assert.match(communitiesSource, /账号加密保存且不回显/)
  assert.doesNotMatch(communitiesSource, /qmf_community_code\}00/)
})

test('residence lookup scope supports multiple communities without exposing credentials', () => {
  assert.match(apiSource, /login_community_ids: number\[\]/)
  assert.match(apiSource, /login_community_names: string\[\]/)
  assert.match(apiSource, /login_community_ids: number\[\]/g)
  assert.match(settingsSource, /mode="multiple"/)
  assert.match(settingsSource, /value=\{residenceConfig\.login_community_ids\}/)
  assert.match(settingsSource, /login_community_ids: value/)
  assert.match(settingsSource, /maxTagCount|responsive/)
  assert.doesNotMatch(settingsSource, /username: residenceConfig\.username/)
  assert.doesNotMatch(settingsSource, /access_token: residenceConfig/)
})

test('manual residence scan is a tracked background job with passive progress polling', () => {
  assert.match(apiSource, /startResidencePlatformScan[\s\S]*run: ExternalAcquisitionRun[\s\S]*reused: boolean/)
  assert.match(settingsSource, /getLatestExternalAcquisitionRun\('residence_full_scan', \{ passive: true \}\)/)
  assert.match(settingsSource, /getExternalAcquisitionRun\(residenceRun\.id, \{ passive: true \}\)/)
  assert.match(settingsSource, /current && current\.id > run\.id/)
  assert.match(settingsSource, /current\?\.id === run\.id \? run : current/)
  assert.match(settingsSource, /查询任务 #\$\{result\.run\.id\} 已进入后台队列/)
  assert.match(settingsSource, /\$\{residenceRun\.current\}\/\$\{residenceRun\.total\}/)
  assert.match(taskQueueSource, /querying: '正在查询'/)
  assert.doesNotMatch(settingsSource, /result\.queued_count/)
})
