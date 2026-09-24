import { unzipSync, zipSync, strToU8, strFromU8 } from 'fflate'
import { identityHeaderScore, selectIdentityColumn } from './offlineResidenceHeaders'

export interface OfflineWorkbook {
  sheetName: string
  header: string[]
  rows: string[][]
  identityColumn: number
}

function xmlEscape(value: string): string {
  return value.replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;',
  }[character] || character))
}

function columnNumber(reference: string): number {
  const letters = reference.match(/[A-Z]+/i)?.[0]?.toUpperCase() || 'A'
  let result = 0
  for (const character of letters) result = result * 26 + character.charCodeAt(0) - 64
  return result - 1
}

function cellValue(cell: Element, shared: string[]): string {
  const type = cell.getAttribute('t') || ''
  if (type === 'inlineStr') return cell.querySelector('is t')?.textContent || ''
  const value = cell.querySelector('v')?.textContent || ''
  if (type === 's') return shared[Number(value)] || ''
  if (type === 'b') return value === '1' ? 'TRUE' : 'FALSE'
  return value
}

function parseSheet(xml: string, shared: string[]): string[][] {
  const document = new DOMParser().parseFromString(xml, 'application/xml')
  const result: string[][] = []
  for (const row of Array.from(document.querySelectorAll('sheetData > row'))) {
    const cells = Array.from(row.querySelectorAll(':scope > c'))
    const values: string[] = []
    for (const cell of cells) {
      const index = columnNumber(cell.getAttribute('r') || 'A1')
      while (values.length <= index) values.push('')
      values[index] = cellValue(cell, shared)
    }
    result.push(values)
  }
  return result
}

function firstSheet(files: Record<string, Uint8Array>): { name: string; path: string } {
  const workbook = new DOMParser().parseFromString(strFromU8(files['xl/workbook.xml']), 'application/xml')
  const relation = new DOMParser().parseFromString(strFromU8(files['xl/_rels/workbook.xml.rels']), 'application/xml')
  const sheet = workbook.querySelector('sheets > sheet')
  const relationId = sheet?.getAttribute('r:id') || ''
  const target = Array.from(relation.querySelectorAll('Relationship')).find(item => item.getAttribute('Id') === relationId)?.getAttribute('Target') || 'worksheets/sheet1.xml'
  return { name: sheet?.getAttribute('name') || 'Sheet1', path: `xl/${target.replace(/^\//, '')}` }
}

export async function readOfflineWorkbook(file: File): Promise<OfflineWorkbook> {
  const files = unzipSync(new Uint8Array(await file.arrayBuffer()))
  const sharedDocument = files['xl/sharedStrings.xml']
    ? new DOMParser().parseFromString(strFromU8(files['xl/sharedStrings.xml']), 'application/xml')
    : null
  const shared = sharedDocument
    ? Array.from(sharedDocument.querySelectorAll('si')).map(item => Array.from(item.querySelectorAll('t')).map(text => text.textContent || '').join(''))
    : []
  const sheet = firstSheet(files)
  const rows = parseSheet(strFromU8(files[sheet.path]), shared)
  let headerIndex = -1
  let identityColumn = -1
  let identityScore = -1
  for (let index = 0; index < Math.min(rows.length, 20); index += 1) {
    const candidate = selectIdentityColumn(rows[index])
    const candidateScore = candidate >= 0 ? identityHeaderScore(rows[index][candidate]) : -1
    if (candidate >= 0 && candidateScore > identityScore) {
      headerIndex = index
      identityColumn = candidate
      identityScore = candidateScore
    }
  }
  if (headerIndex < 0) throw new Error('未找到身份证号列（支持：身份证号、身份证号码、证件号码、公民身份号码、身份证）')
  const header = [...rows[headerIndex]]
  while (header.length && !header[header.length - 1]) header.pop()
  const data = rows.slice(headerIndex + 1)
    .filter(row => row.some(value => String(value || '').trim()))
    .map(row => {
      const normalized = [...row]
      while (normalized.length < header.length) normalized.push('')
      return normalized.slice(0, header.length)
    })
  return { sheetName: sheet.name, header, rows: data, identityColumn }
}

function columnName(index: number): string {
  let value = index + 1
  let result = ''
  while (value > 0) {
    const remainder = (value - 1) % 26
    result = String.fromCharCode(65 + remainder) + result
    value = Math.floor((value - 1) / 26)
  }
  return result
}

export interface OfflineWorkbookOutput {
  /** Existing status mode inserts a result immediately after the identity column. */
  mode?: 'status' | 'address'
  addresses?: string[]
}

export function writeOfflineWorkbook(book: OfflineWorkbook, statuses: string[], output: OfflineWorkbookOutput = {}): Blob {
  const header = [...book.header]
  const rows = book.rows.map(row => [...row])
  if (output.mode === 'address') {
    header.push('登记地址')
    rows.forEach((row, index) => row.push(output.addresses?.[index] || ''))
  } else {
    header.splice(book.identityColumn + 1, 0, '登记情况')
    rows.forEach((row, index) => row.splice(book.identityColumn + 1, 0, statuses[index] || ''))
  }
  const allRows = [header, ...rows]
  const sheetRows = allRows.map((row, rowIndex) => {
    const cells = row.map((value, columnIndex) => {
      const ref = `${columnName(columnIndex)}${rowIndex + 1}`
      return `<c r="${ref}" t="inlineStr"><is><t xml:space="preserve">${xmlEscape(String(value ?? ''))}</t></is></c>`
    }).join('')
    return `<row r="${rowIndex + 1}">${cells}</row>`
  }).join('')
  const sheet = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><sheetData>${sheetRows}</sheetData><autoFilter ref="A1:${columnName(header.length - 1)}${allRows.length}"/></worksheet>`
  const files = {
    '[Content_Types].xml': strToU8('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'),
    '_rels/.rels': strToU8('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'),
    'xl/workbook.xml': strToU8(`<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="${xmlEscape(book.sheetName.slice(0, 31) || 'Sheet1')}" sheetId="1" r:id="rId1"/></sheets></workbook>`),
    'xl/_rels/workbook.xml.rels': strToU8('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'),
    'xl/worksheets/sheet1.xml': strToU8(sheet),
  }
  return new Blob([zipSync(files, { level: 6 })], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })
}
