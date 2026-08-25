import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  buildSummaryWorkbook,
  normalizeXlsxFileName,
} from '../src/utils/summaryXlsx.ts'

test('汇总工作簿只导出当前可见行并保留数字格式', () => {
  const workbook = buildSummaryWorkbook({
    fileName: '在线数据汇总:全链条/2026-08-03',
    tables: [{
      sheet: '网格员汇总',
      columns: ['id', '社区', '姓名', '已完成', '核查完成率', '_person_days_exact'],
      rows: [{
        id: 2,
        社区: '长板',
        姓名: '测试人员',
        已完成: 3,
        核查完成率: 0.75,
        _person_days_exact: 1,
      }],
      total: {
        社区: '总计',
        姓名: '',
        已完成: 3,
        核查完成率: 0.75,
      },
    }],
  })

  assert.equal(workbook.fileName, '在线数据汇总_全链条_2026-08-03.xlsx')
  const sheet = workbook.sheets[0]
  assert.deepEqual(
    sheet.data[0].map(cell => typeof cell === 'object' && cell ? cell.value : cell),
    ['社区', '姓名', '已完成', '核查完成率'],
  )
  assert.equal((sheet.data[1][2] as { value: number }).value, 3)
  assert.equal((sheet.data[1][3] as { format: string }).format, '0.0%')
  assert.equal((sheet.data[2][0] as { value: string }).value, '总计')
  assert.equal((sheet.data[2][0] as { fontWeight: string }).fontWeight, 'bold')
})

test('导出文件名始终使用 XLSX 扩展名', () => {
  assert.equal(normalizeXlsxFileName('走访汇总.xlsx'), '走访汇总.xlsx')
  assert.equal(normalizeXlsxFileName('   '), '汇总数据.xlsx')
})

test('走访社区汇总把网格员人数放在在岗人日前并按整数导出', () => {
  const workbook = buildSummaryWorkbook({
    fileName: '走访汇总',
    tables: [{
      sheet: '社区汇总',
      columns: ['社区', '走访户数', '网格员人数', '在岗人日'],
      rows: [{ 社区: '长板', 走访户数: 12, 网格员人数: 4, 在岗人日: 3.5 }],
      total: { 社区: '总计', 走访户数: 12, 网格员人数: 4, 在岗人日: 3.5 },
    }],
  })
  const sheet = workbook.sheets[0]
  assert.deepEqual(
    sheet.data[0].map(cell => typeof cell === 'object' && cell ? cell.value : cell),
    ['社区', '走访户数', '网格员人数', '在岗人日'],
  )
  assert.equal((sheet.data[1][2] as { format: string }).format, '#,##0')
  assert.equal((sheet.data[1][3] as { format: string }).format, '0.0')
})

test('走访社区汇总导出将三项最低指标各标黄三个单元格', () => {
  const workbook = buildSummaryWorkbook({
    fileName: '走访汇总',
    tables: [{
      sheet: '社区汇总',
      columns: ['社区', '人均日走访户数', '人均日变动数', '户均变动数'],
      rows: [
        { 社区: '甲', 人均日走访户数: 4, 人均日变动数: 2, 户均变动数: 0.9 },
        { 社区: '乙', 人均日走访户数: 1, 人均日变动数: 5, 户均变动数: 0.3 },
        { 社区: '丙', 人均日走访户数: 3, 人均日变动数: 1, 户均变动数: 0.7 },
        { 社区: '丁', 人均日走访户数: 2, 人均日变动数: 4, 户均变动数: 0.2 },
      ],
      highlightLowestColumns: [
        '人均日走访户数',
        '人均日变动数',
        '户均变动数',
      ],
      total: { 社区: '总计' },
    }],
  })
  const sheet = workbook.sheets[0]
  const yellowCells = sheet.data
    .slice(1, -1)
    .flatMap(row => row.filter(cell => (
      typeof cell === 'object'
      && cell
      && 'backgroundColor' in cell
      && cell.backgroundColor === '#fff2cc'
    )))
  assert.equal(yellowCells.length, 9)
})

test('在线和走访汇总导出前都会写入操作记录', () => {
  for (const page of ['Dashboard.tsx', 'VisitSummary.tsx']) {
    const source = readFileSync(
      new URL(`../src/pages/${page}`, import.meta.url),
      'utf8',
    )
    assert.match(source, /await recordXlsxExport\([\s\S]*await exportSummaryWorkbook\(/)
  }
})
