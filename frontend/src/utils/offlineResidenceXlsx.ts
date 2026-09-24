import { unzipSync, zipSync, strToU8, strFromU8 } from 'fflate'
import { identityHeaderScore, selectIdentityColumn } from './offlineResidenceHeaders'

export interface OfflineWorkbook {
  sheetName: string
  header: string[]
  rows: string[][]
  identityColumn: number
  /** Original package and physical row positions are retained for address-only edits. */
  source?: { files: Record<string, Uint8Array>; sheetPath: string; headerRow: number; dataRows: number[] }
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

function parseSheet(xml: string, shared: string[]): Array<{ number: number; values: string[] }> {
  const document = new DOMParser().parseFromString(xml, 'application/xml')
  const result: Array<{ number: number; values: string[] }> = []
  for (const row of Array.from(document.querySelectorAll('sheetData > row'))) {
    const cells = Array.from(row.querySelectorAll(':scope > c'))
    const values: string[] = []
    for (const cell of cells) {
      const index = columnNumber(cell.getAttribute('r') || 'A1')
      while (values.length <= index) values.push('')
      values[index] = cellValue(cell, shared)
    }
    result.push({ number: Number(row.getAttribute('r')), values })
  }
  return result
}

function firstSheet(files: Record<string, Uint8Array>): { name: string; path: string } {
  const workbook = new DOMParser().parseFromString(strFromU8(files['xl/workbook.xml']), 'application/xml')
  const relation = new DOMParser().parseFromString(strFromU8(files['xl/_rels/workbook.xml.rels']), 'application/xml')
  const sheet = workbook.querySelector('sheets > sheet')
  const relationId = sheet?.getAttribute('r:id') || ''
  const target = Array.from(relation.querySelectorAll('Relationship')).find(item => item.getAttribute('Id') === relationId)?.getAttribute('Target') || 'worksheets/sheet1.xml'
  const path = target.startsWith('/') ? target.slice(1) : `xl/${target.replace(/^\.\.\//, '')}`
  return { name: sheet?.getAttribute('name') || 'Sheet1', path }
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
  if (!files[sheet.path]) throw new Error('工作簿中找不到目标工作表')
  const rows = parseSheet(strFromU8(files[sheet.path]), shared)
  let headerIndex = -1
  let identityColumn = -1
  let identityScore = -1
  for (let index = 0; index < Math.min(rows.length, 20); index += 1) {
    const candidate = selectIdentityColumn(rows[index].values)
    const candidateScore = candidate >= 0 ? identityHeaderScore(rows[index].values[candidate]) : -1
    if (candidate >= 0 && candidateScore > identityScore) {
      headerIndex = index
      identityColumn = candidate
      identityScore = candidateScore
    }
  }
  if (headerIndex < 0) throw new Error('未找到身份证号列（支持：身份证号、身份证号码、证件号码、公民身份号码、身份证）')
  const header = [...rows[headerIndex].values]
  while (header.length && !header[header.length - 1]) header.pop()
  const dataRows = rows.slice(headerIndex + 1)
    .filter(row => row.values.some(value => String(value || '').trim()))
  const data = dataRows.map(row => {
      const normalized = [...row.values]
      while (normalized.length < header.length) normalized.push('')
      return normalized.slice(0, header.length)
    })
  return { sheetName: sheet.name, header, rows: data, identityColumn,
    source: { files, sheetPath: sheet.path, headerRow: rows[headerIndex].number, dataRows: dataRows.map(row => row.number) } }
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

function writeAddressIntoSource(book: OfflineWorkbook, addresses: string[]): Blob {
  const source = book.source
  if (!source || !Number.isInteger(source.headerRow) || source.dataRows.some(row => !Number.isInteger(row))) {
    throw new Error('源工作簿缺少可核对的行位置，无法安全保留原格式')
  }
  if (addresses.length !== book.rows.length || source.dataRows.length !== book.rows.length) {
    throw new Error('查询结果与源工作簿行数不一致，已停止导出')
  }
  const matches = book.header.flatMap((value, index) => value.trim() === '登记地址' ? [index] : [])
  if (matches.length > 1) throw new Error('源表存在多个“登记地址”列，请先核对表头')
  const document = new DOMParser().parseFromString(strFromU8(source.files[source.sheetPath]), 'application/xml')
  if (document.querySelector('parsererror')) throw new Error('源工作表 XML 无法解析')
  const sheetData = document.querySelector('sheetData')
  if (!sheetData) throw new Error('源工作表缺少数据区域')
  const allCells = Array.from(sheetData.querySelectorAll('c'))
  const lastColumn = allCells.reduce((maximum, cell) => Math.max(maximum, columnNumber(cell.getAttribute('r') || 'A1')), book.header.length - 1)
  const column = matches[0] ?? lastColumn + 1
  if (column >= 16384) throw new Error('工作表已达到 Excel 最大列数，无法新增登记地址')
  const ref = columnName(column)
  const rowByNumber = new Map(Array.from(sheetData.querySelectorAll('row')).map(row => [Number(row.getAttribute('r')), row]))
  const put = (number: number, value: string) => {
    const row = rowByNumber.get(number)
    if (!row) throw new Error('源工作表行位置已变化，无法安全导出')
    const cellRef = `${ref}${number}`
    let cell = Array.from(row.querySelectorAll(':scope > c')).find(item => item.getAttribute('r') === cellRef)
    if (!cell) {
      cell = document.createElementNS(sheetData.namespaceURI, 'c')
      cell.setAttribute('r', cellRef)
      const preceding = Array.from(row.querySelectorAll(':scope > c')).filter(item => columnNumber(item.getAttribute('r') || 'A1') < column)
      const previous = preceding[preceding.length - 1]
      if (previous?.hasAttribute('s')) cell.setAttribute('s', previous.getAttribute('s') || '')
      const next = Array.from(row.querySelectorAll(':scope > c')).find(item => columnNumber(item.getAttribute('r') || 'A1') > column)
      row.insertBefore(cell, next || null)
    }
    while (cell.firstChild) cell.removeChild(cell.firstChild)
    cell.setAttribute('t', 'inlineStr')
    const inline = document.createElementNS(sheetData.namespaceURI, 'is')
    const text = document.createElementNS(sheetData.namespaceURI, 't')
    text.setAttribute('xml:space', 'preserve')
    text.textContent = value
    inline.appendChild(text)
    cell.appendChild(inline)
  }
  put(source.headerRow, '登记地址')
  source.dataRows.forEach((number, index) => put(number, addresses[index] || ''))
  if (matches.length === 0) {
    const dimension = document.querySelector('dimension')
    const dimensionRef = dimension?.getAttribute('ref')
    if (dimensionRef) dimension?.setAttribute('ref', dimensionRef.replace(/([A-Z]+)(\d+)$/, `${ref}$2`))
    const filter = document.querySelector('autoFilter')
    const filterRef = filter?.getAttribute('ref')
    if (filterRef) filter?.setAttribute('ref', filterRef.replace(/([A-Z]+)(\d+)$/, `${ref}$2`))
  }
  return new Blob([zipSync({ ...source.files, [source.sheetPath]: strToU8(new XMLSerializer().serializeToString(document)) }, { level: 6 })],
    { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })
}

export function writeOfflineWorkbook(book: OfflineWorkbook, statuses: string[], output: OfflineWorkbookOutput = {}): Blob {
  if (output.mode === 'address') return writeAddressIntoSource(book, output.addresses || [])
  const header = [...book.header]
  const rows = book.rows.map(row => [...row])
  header.splice(book.identityColumn + 1, 0, '登记情况')
  rows.forEach((row, index) => row.splice(book.identityColumn + 1, 0, statuses[index] || ''))
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
