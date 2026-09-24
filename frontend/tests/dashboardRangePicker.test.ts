import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { updateDateRangePickerSelection } from '../src/utils/dateRangePicker.ts'

test('选择在线数据汇总起始日期时丢弃旧结束日期并保留半成品范围', () => {
  assert.deepEqual(
    updateDateRangePickerSelection(['2026-09-01', '2026-09-23'], 'start'),
    ['2026-09-01', ''],
  )
})

test('选择结束日期时保留完整范围，供提交回调使用', () => {
  assert.deepEqual(
    updateDateRangePickerSelection(['2026-09-01', '2026-09-23'], 'end'),
    ['2026-09-01', '2026-09-23'],
  )
})

test('在线数据汇总使用半成品范围驱动日期控件但只提交完整范围', () => {
  const source = readFileSync(
    new URL('../src/pages/Dashboard.tsx', import.meta.url),
    'utf8',
  )
  assert.match(source, /const \[pickerRange, setPickerRange\]/)
  assert.match(source, /value=\{pickerValue\}/)
  assert.match(source, /onCalendarChange=\{\(_, dateStrings, info\) =>/)
  assert.match(source, /updateDateRangePickerSelection\(dateStrings, info\?\.range\)/)
  assert.match(source, /onOpenChange=\{\(open\) =>[\s\S]*if \(!open\) setPickerRange\(null\)/)
  assert.match(source, /order=\{false\}/)
  assert.match(source, /if \(dateStrings\[0\] && dateStrings\[1\]\)[\s\S]*setDateRange/)
})
