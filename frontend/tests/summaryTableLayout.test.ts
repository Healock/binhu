import assert from 'node:assert/strict'
import test from 'node:test'

import {
  visitSummaryColumnWidth,
  visitSummaryScrollWidth,
} from '../src/utils/summaryTableLayout.ts'

test('走访汇总表头与表体复用同一组明确列宽', () => {
  const columns = ['社区', '走访户数', '在岗人日', '人均日走访户数', '新增']
  assert.deepEqual(columns.map(visitSummaryColumnWidth), [120, 112, 112, 136, 112])
  assert.equal(visitSummaryScrollWidth(columns), 592)
})
