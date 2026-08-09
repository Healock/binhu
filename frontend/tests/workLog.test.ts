import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  deriveWorkLogValues,
  leafWorkLogColumns,
} from '../src/utils/workLog.ts'

test('工作日志自动表格会重新计算概览数字', () => {
  const values = deriveWorkLogValues({
    'flow.instruction_table': [{
      grid_member_count: 2,
      total: 10,
      unchecked: 4,
      checked: 6,
      unable: 1,
    }],
    'rental.visit_table': [{
      grid_member_count: 2,
      visits: 8,
      added: 1,
      changed: 3,
      cancelled: 0,
      rated: 4,
    }],
  })
  assert.equal(values['flow.instruction.completion_rate'], 60)
  assert.equal(values['flow.instruction.ground_rate'], 83.3)
  assert.equal(values['rental.visit.average_visits'], 4)
  assert.equal(values['rental.visit.rating_rate'], 50)
})

test('工作日志分组表头可以展开为实际填写列', () => {
  const columns = leafWorkLogColumns([
    { key: 'community', label: '社区' },
    {
      label: '回访阶段',
      children: [
        { key: 'pending', label: '待回访' },
        { key: 'resolved', label: '已化解' },
      ],
    },
  ])
  assert.deepEqual(
    columns.map(column => column.key),
    ['community', 'pending', 'resolved'],
  )
})

test('文件生成页面用独立 Panel 区分每日明细和工作日志', () => {
  const source = readFileSync(
    new URL('../src/pages/WorkLog.tsx', import.meta.url),
    'utf8',
  )
  assert.match(source, /title="文件生成"/)
  assert.match(source, /title="工作每日明细"/)
  assert.match(source, /title="工作日志"/)
  assert.match(source, /出租房目标数/)
  assert.match(source, /自购房目标数/)
  assert.match(source, /生成每日明细 XLSX/)
  assert.match(source, /exportWorkLogDailyDetail/)
  assert.match(source, /dailyDetailDate/)
  assert.match(source, /setDailyDetailDate/)
  assert.match(
    source,
    /title="工作每日明细"[\s\S]*?业务日期[\s\S]*?value=\{dailyDetailDate\}/,
  )
  assert.match(
    source,
    /title="工作日志"[\s\S]*?日报日期[\s\S]*?value=\{businessDate\}/,
  )
  assert.ok(
    source.indexOf('title="工作每日明细"')
      < source.indexOf('title="工作日志"'),
  )
})
