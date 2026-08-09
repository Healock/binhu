import assert from 'node:assert/strict'
import test from 'node:test'
import {
  buildReportTableTotal,
  buildVisitTableTotal,
} from '../src/utils/tableTotals.ts'

test('在线汇总总计只使用筛选后的行', () => {
  const columns = [
    '社区',
    '数据总数',
    '未核查',
    '已核查',
    '已完成',
    '核查完成率',
    '无法见底数',
    '核查见底率',
  ]
  const filteredRows = [
    {
      社区: '长板',
      数据总数: 10,
      未核查: 2,
      已核查: 3,
      已完成: 5,
      无法见底数: 1,
    },
  ]

  const total = buildReportTableTotal(columns, filteredRows)
  assert.equal(total.社区, '总计')
  assert.equal(total.数据总数, 10)
  assert.equal(total.核查完成率, 0.5)
  assert.equal(total.核查见底率, 0.83)
})

test('在线汇总两列模式按已核查计算完成率', () => {
  const columns = [
    '社区',
    '数据总数',
    '未核查',
    '已核查',
    '核查完成率',
    '无法见底数',
    '核查见底率',
  ]
  const total = buildReportTableTotal(columns, [
    {
      社区: '长板',
      数据总数: 10,
      未核查: 6,
      已核查: 4,
      无法见底数: 1,
    },
  ])

  assert.equal(total.未核查, 6)
  assert.equal(total.已核查, 4)
  assert.equal(total.核查完成率, 0.4)
  assert.equal(total.核查见底率, 0.8)
})

test('在线区间汇总按在岗人日重算每日人均核查数', () => {
  const columns = [
    '社区',
    '已完成',
    '在岗人日',
    '每日人均核查数',
  ]
  const total = buildReportTableTotal(columns, [
    { 社区: '长板', 已完成: 12, 在岗人日: 3, 每日人均核查数: 4 },
    { 社区: '龙河', 已完成: 8, 在岗人日: 2, 每日人均核查数: 4 },
  ])

  assert.equal(total.已完成, 20)
  assert.equal(total.在岗人日, 5)
  assert.equal(total.每日人均核查数, 4)
})

test('双休日排班不完整时在线汇总不猜测每日人均核查数', () => {
  const total = buildReportTableTotal(
    ['社区', '已完成', '在岗人日', '每日人均核查数'],
    [{ 社区: '长板', 已完成: 12, 在岗人日: null, 每日人均核查数: null }],
  )

  assert.equal(total.每日人均核查数, null)
})

test('走访汇总总计按筛选行和在岗人日重算比率', () => {
  const columns = [
    '社区',
    '走访户数',
    '网格员人数',
    '在岗人日',
    '人均日走访户数',
    '新增',
    '变更',
    '注销',
    '总变动数',
    '人均日变动数',
    '户均变动数',
    '星级评定数',
    '星级评定率',
  ]
  const total = buildVisitTableTotal(columns, [
    {
      社区: '长板',
      走访户数: 9,
      网格员人数: 4,
      在岗人日: 3,
      _person_days_exact: 2.95,
      人均日走访户数: 3,
      新增: 2,
      变更: 3,
      注销: 1,
      总变动数: 6,
      人均日变动数: 2,
      星级评定数: 3,
    },
  ])

  assert.equal(total.走访户数, 9)
  assert.equal(total.网格员人数, 4)
  assert.equal(total.总变动数, 6)
  assert.equal(total.在岗人日, 3)
  assert.equal(total.人均日走访户数, 3.1)
  assert.equal(total.人均日变动数, 2)
  assert.equal(total.户均变动数, 0.7)
  assert.equal(total.星级评定率, 0.3333)
})

test('周末排班不完整时筛选总计不伪造人均值', () => {
  const total = buildVisitTableTotal(
    ['社区', '走访户数', '在岗人日', '人均日走访户数', '人均日变动数'],
    [{
      社区: '长板',
      走访户数: 9,
      在岗人日: 2,
      人均日走访户数: null,
      人均日变动数: null,
    }],
  )
  assert.equal(total.在岗人日, 2)
  assert.equal(total.人均日走访户数, null)
  assert.equal(total.人均日变动数, null)
})

test('空筛选结果显示全零总计', () => {
  const total = buildVisitTableTotal(
    ['社区', '走访户数', '户均变动数', '星级评定率'],
    [],
  )
  assert.equal(total.社区, '总计')
  assert.equal(total.走访户数, 0)
  assert.equal(total.户均变动数, 0)
  assert.equal(total.星级评定率, 0)
})
