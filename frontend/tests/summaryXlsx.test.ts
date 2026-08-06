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

test('在线和走访汇总导出前都会写入操作记录', () => {
  for (const page of ['Dashboard.tsx', 'VisitSummary.tsx']) {
    const source = readFileSync(
      new URL(`../src/pages/${page}`, import.meta.url),
      'utf8',
    )
    assert.match(source, /await recordXlsxExport\([\s\S]*await exportSummaryWorkbook\(/)
  }
})
