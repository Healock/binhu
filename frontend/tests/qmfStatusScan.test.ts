import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const apiSource = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8')
const listSource = readFileSync(new URL('../src/pages/MobileTaskList.tsx', import.meta.url), 'utf8')
const tableSource = readFileSync(new URL('../src/components/MobileTaskTable.tsx', import.meta.url), 'utf8')
const statusSource = readFileSync(new URL('../src/components/QmfFeedbackStatus.tsx', import.meta.url), 'utf8')
const settingsSource = readFileSync(new URL('../src/pages/SystemSettings.tsx', import.meta.url), 'utf8')
const detailSource = readFileSync(new URL('../src/pages/MobileTaskDetail.tsx', import.meta.url), 'utf8')

test('全民防反馈扫描接口和七类安全状态已接入', () => {
  assert.match(apiSource, /post\('\/qmf-registration\/status-scans'/)
  assert.match(apiSource, /get\('\/qmf-registration\/status-scans\/latest'/)
  for (const state of [
    'not_scanned',
    'stale',
    'pending',
    'completed_match',
    'completed_mismatch',
    'not_found',
    'error',
  ]) {
    assert.match(statusSource, new RegExp(state))
  }
})

test('模型三列表同时展示扫描进度、筛选和卡片表格状态', () => {
  assert.match(listSource, /全量核对全民防反馈/)
  assert.match(listSource, /import \{[^}]*Modal[^}]*\} from 'antd'/s)
  assert.match(listSource, /Modal\.confirm\(/)
  assert.match(listSource, /qmf_feedback_states: qmfFeedbackStates/)
  assert.match(listSource, /<Progress/)
  assert.match(listSource, /<QmfFeedbackStatus status=\{task\.qmf_status\}/)
  assert.match(tableSource, /<QmfFeedbackStatus status=\{task\.qmf_status\}/)
  assert.match(listSource, /qmf-scan-result-filters/)
  assert.match(listSource, /selectQmfFeedbackResult/)
  assert.match(listSource, /completed_match.*一致/)
  assert.match(listSource, /completed_mismatch.*不一致/)
  assert.match(listSource, /pending.*未核查/)
  assert.match(listSource, /not_found.*无记录/)
  assert.match(listSource, /error.*异常/)
  assert.match(listSource, /setStatus\('completed'\)/)
})

test('每日扫描设置默认由后台配置并使用上海时区时间', () => {
  assert.match(settingsSource, /每日反馈扫描/)
  assert.match(settingsSource, /type="time"/)
  assert.match(settingsSource, /Asia\/Shanghai/)
  assert.match(settingsSource, /status_scan_enabled/)
  assert.match(settingsSource, /status_scan_time/)
})

test('全民防设置只保留外部只读查询配置', () => {
  assert.doesNotMatch(settingsSource, /登录协议已实测/)
  assert.doesNotMatch(settingsSource, /写入协议已实测/)
  assert.doesNotMatch(settingsSource, /登记前核对与全民防登记均已开启/)
  assert.doesNotMatch(settingsSource, /全民防登记开关|二次确认|上传照片、保存人员资料并反馈模型三/)
  assert.match(settingsSource, /全民防模型三只读查询/)
  assert.match(settingsSource, /不会上传照片、保存人员、反馈结果或执行真实登记/)
  assert.match(settingsSource, /registration_enabled: false/)
})

test('任务详情实时复核后立即替换旧的全民防缓存状态', () => {
  assert.match(detailSource, /function realtimeQmfSnapshot/)
  assert.match(detailSource, /completed_match: 'completed_match'/)
  assert.match(detailSource, /completed_mismatch: 'completed_mismatch'/)
  assert.match(detailSource, /task: \{ \.\.\.current\.task, qmf_status: snapshot \}/)
  assert.match(detailSource, /qmf_status: snapshot/)
})
