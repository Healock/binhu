import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import test from 'node:test'
import { build } from 'esbuild'
import { strFromU8, strToU8, unzipSync, zipSync } from 'fflate'
import { chromium } from 'playwright'

const chrome = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const CONTENT_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
const xml = (body: string) => `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>${body}`

function fixture(): Uint8Array {
  return zipSync({
    '[Content_Types].xml': strToU8(xml('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')),
    '_rels/.rels': strToU8(xml('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')),
    'xl/workbook.xml': strToU8(xml('<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="虚构测试" sheetId="1" r:id="rId1"/><sheet name="公式保留" sheetId="2" r:id="rId2"/></sheets></workbook>')),
    'xl/_rels/workbook.xml.rels': strToU8(xml('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/></Relationships>')),
    'xl/styles.xml': strToU8(xml('<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="1"><font><b/></font></fonts><fills count="1"><fill><patternFill patternType="none"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="2"><xf/><xf fontId="0" applyFont="1"/></cellXfs></styleSheet>')),
    'xl/worksheets/sheet1.xml': strToU8(xml('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:J5"/><sheetViews><sheetView workbookViewId="0"><pane ySplit="2" topLeftCell="A3" state="frozen"/></sheetView></sheetViews><cols><col min="2" max="2" width="24" customWidth="1"/></cols><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>虚构标题</t></is></c></row><row r="2"><c r="A2" s="1" t="inlineStr"><is><t>姓名</t></is></c><c r="B2" s="1" t="inlineStr"><is><t>身份证号</t></is></c><c r="J2" s="1" t="inlineStr"><is><t>备注</t></is></c></row><row r="3"><c r="A3" t="inlineStr"><is><t>虚构甲</t></is></c><c r="B3" t="inlineStr"><is><t>11010519491231002X</t></is></c><c r="J3" t="inlineStr"><is><t>原备注</t></is></c></row><row r="4"/><row r="5"><c r="A5" t="inlineStr"><is><t>虚构乙</t></is></c><c r="B5" t="inlineStr"><is><t>11010519491231002X</t></is></c></row></sheetData><autoFilter ref="A2:J5"/></worksheet>')),
    'xl/worksheets/sheet2.xml': strToU8(xml('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1"><f>1+1</f><v>2</v></c></row></sheetData></worksheet>')),
  })
}

test('地址导出保留源结构并在二次导入时更新原列', { skip: !existsSync(chrome) }, async () => {
  const bundle = await build({ entryPoints: ['src/utils/offlineResidenceXlsx.ts'], bundle: true, write: false, platform: 'browser', format: 'iife', globalName: 'OfflineXlsx' })
  const browser = await chromium.launch({ executablePath: chrome, headless: true })
  try {
    const page = await browser.newPage()
    await page.setContent('<!doctype html><html><body></body></html>')
    await page.addScriptTag({ content: bundle.outputFiles[0].text })
    const original = fixture()
    const first = await page.evaluate(async ({ bytes, type }) => {
      const file = new File([new Uint8Array(bytes)], 'fixture.xlsx', { type })
      const book = await (window as any).OfflineXlsx.readOfflineWorkbook(file)
      if (book.rows.length !== 2) throw new Error('数据行对应关系错误')
      const output = (window as any).OfflineXlsx.writeOfflineWorkbook(book, [], { mode: 'address', addresses: ['虚构路1号', '虚构路2号'] })
      return Array.from(new Uint8Array(await output.arrayBuffer()))
    }, { bytes: Array.from(original), type: CONTENT_TYPE })
    const firstZip = unzipSync(new Uint8Array(first))
    const originalZip = unzipSync(original)
    for (const path of Object.keys(originalZip).filter(path => path !== 'xl/worksheets/sheet1.xml')) {
      assert.deepEqual(firstZip[path], originalZip[path], `未修改的 ${path} 应逐字节保留`)
    }
    const sheet = strFromU8(firstZip['xl/worksheets/sheet1.xml'])
    for (const fragment of ['width="24"', 'ySplit="2"', 'ref="A2:K5"', 'r="K2"', 'r="K3"', 'r="K5"', '虚构路1号', '虚构路2号', '<row r="4"']) assert.ok(sheet.includes(fragment), fragment)
    const second = await page.evaluate(async ({ bytes, type }) => {
      const book = await (window as any).OfflineXlsx.readOfflineWorkbook(new File([new Uint8Array(bytes)], 'again.xlsx', { type }))
      const output = (window as any).OfflineXlsx.writeOfflineWorkbook(book, [], { mode: 'address', addresses: ['复查路3号', ''] })
      return Array.from(new Uint8Array(await output.arrayBuffer()))
    }, { bytes: first, type: CONTENT_TYPE })
    const secondSheet = strFromU8(unzipSync(new Uint8Array(second))['xl/worksheets/sheet1.xml'])
    assert.equal((secondSheet.match(/登记地址/g) || []).length, 1)
    assert.ok(secondSheet.includes('复查路3号'))
    assert.ok(!secondSheet.includes('虚构路1号'))
    assert.ok(!secondSheet.includes('r="L2"'))
    assert.ok(secondSheet.includes('r="K5"'))
  } finally { await browser.close() }
})
