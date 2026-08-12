import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { formatUTCTime } from '../src/api/client.ts'

test('无时区标记的数据库时间按 UTC 转为系统配置时区', () => {
  assert.equal(
    formatUTCTime('2026-08-12T05:52:00', 'Asia/Shanghai'),
    '2026-08-12 13:52:00',
  )
  assert.equal(
    formatUTCTime('2026-08-12 05:52:00', 'Asia/Tokyo'),
    '2026-08-12 14:52:00',
  )
})

test('全局时区来自启动接口并可在系统设置保存后更新', () => {
  const authSource = readFileSync(
    new URL('../src/context/AuthContext.tsx', import.meta.url),
    'utf8',
  )
  const settingsSource = readFileSync(
    new URL('../src/pages/SystemSettings.tsx', import.meta.url),
    'utf8',
  )

  assert.match(authSource, /payload\.timezone/)
  assert.match(authSource, /systemTimezone/)
  assert.match(settingsSource, /setSystemTimezone\(timezone\)/)
})

test('照片工单搜索使用即时筛选并按系统时区显示时间', () => {
  const workflowSource = readFileSync(
    new URL('../src/pages/WorkflowTickets.tsx', import.meta.url),
    'utf8',
  )

  assert.match(workflowSource, /className="workflow-photo-search"/)
  assert.match(workflowSource, /useDebouncedValue\(keyword\.trim\(\), 350, keywordFlush\)/)
  assert.match(workflowSource, /setKeywordFlush\(current => current \+ 1\)/)
  assert.doesNotMatch(workflowSource, />\s*查询\s*<\/Button>/)
  assert.match(workflowSource, /formatUTCTime\(value, systemTimezone\)/)
})
