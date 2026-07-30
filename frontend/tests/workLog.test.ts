import assert from 'node:assert/strict'
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
