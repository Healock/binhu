import { useEffect, useRef } from 'react'
import {
  LogLevel,
  LocaleType,
  merge,
  RedoCommand,
  Univer,
  UndoCommand,
  type IRange,
} from '@univerjs/core'
import { FUniver } from '@univerjs/core/facade'
import { defaultTheme } from '@univerjs/themes'
import {
  AutoFillCommand,
  ClearSelectionAllCommand,
  ClearSelectionContentCommand,
  MoveRangeCommand,
  SetRangeValuesCommand,
  UniverSheetsCorePreset,
  type FWorksheet,
} from '@univerjs/preset-sheets-core'
import sheetsZhCN from '@univerjs/preset-sheets-core/locales/zh-CN'
import { UniverSheetsDataValidationPreset } from '@univerjs/preset-sheets-data-validation'
import validationZhCN from '@univerjs/preset-sheets-data-validation/locales/zh-CN'

import '@univerjs/preset-sheets-core/lib/index.css'
import '@univerjs/preset-sheets-data-validation/lib/index.css'

import type { QueryColumnMeta, QueryDataRow } from '../api/client'
import type { QueryDisplayRow } from '../utils/queryGrid'
import {
  applyQuerySheetValues,
  buildQuerySheetRows,
  canEditQuerySheetCell,
  isQuerySheetRangeEditable,
  parseQuerySheetClipboard,
  selectedQuerySheetRow,
  updateQuerySheetDrafts,
  type QuerySheetCellChange,
  type QuerySheetRow,
} from '../utils/querySpreadsheet'

export interface QuerySpreadsheetProps {
  businessType: string
  source: 'online' | 'archive'
  rows: QueryDataRow[]
  columns: string[]
  columnMeta: QueryColumnMeta[]
  drafts: QueryDisplayRow[]
  canAdd: boolean
  revision: number
  onDraftsChange: (drafts: QueryDisplayRow[]) => void
  onSelectionChange: (row: QueryDisplayRow | null) => void
  onCommit: (changes: QuerySheetCellChange[]) => Promise<void>
  onBlocked: (message: string) => void
  onSavingChange?: (saving: boolean) => void
}

function normalizeCellValue(value: unknown): string {
  return value === null || value === undefined ? '' : String(value)
}

function rangeSize(range: IRange): { rows: number; columns: number } {
  return {
    rows: range.endRow - range.startRow + 1,
    columns: range.endColumn - range.startColumn + 1,
  }
}

function commandRanges(params: any, worksheet: FWorksheet): IRange[] {
  if (Array.isArray(params?.ranges)) return params.ranges
  if (params?.range) return [params.range]
  const active = worksheet.getActiveRange()
  return active ? [active.getRange()] : []
}

function columnWidth(column: string): number {
  if (['地址', '现住址', '核查结果', '核查反馈', '二次反馈', '二次核查结果', '实际情况'].includes(column)) {
    return 240
  }
  if (column.includes('时间') || column.includes('日期')) return 130
  if (column.includes('身份证')) return 180
  return 150
}

function createQueryUniver(
  container: HTMLElement,
  corePreset: ReturnType<typeof UniverSheetsCorePreset>,
  validationPreset: ReturnType<typeof UniverSheetsDataValidationPreset>,
) {
  const univer = new Univer({
    locale: LocaleType.ZH_CN,
    locales: {
      [LocaleType.ZH_CN]: merge({}, sheetsZhCN, validationZhCN),
    },
    theme: defaultTheme,
    logLevel: LogLevel.WARN,
  })
  const plugins = new Map<string, { plugin: any; options?: any }>()
  for (const preset of [corePreset, validationPreset]) {
    for (const entry of preset.plugins) {
      const [plugin, options] = Array.isArray(entry) ? entry : [entry, undefined]
      plugins.set(plugin.pluginName, { plugin, options })
    }
  }
  plugins.forEach(({ plugin, options }) => univer.registerPlugin(plugin, options))
  return { univer, univerAPI: FUniver.newAPI(univer), container }
}

export function QuerySpreadsheet({
  businessType,
  source,
  rows,
  columns,
  columnMeta,
  drafts,
  canAdd,
  revision,
  onDraftsChange,
  onSelectionChange,
  onCommit,
  onBlocked,
  onSavingChange,
}: QuerySpreadsheetProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const callbacksRef = useRef({
    onDraftsChange,
    onSelectionChange,
    onCommit,
    onBlocked,
    onSavingChange,
  })

  callbacksRef.current = {
    onDraftsChange,
    onSelectionChange,
    onCommit,
    onBlocked,
    onSavingChange,
  }

  useEffect(() => {
    const container = containerRef.current
    if (!container || columns.length === 0) return
    container.replaceChildren()

    const generation = `${businessType}-${source}-${revision}`
    const sheetRows = buildQuerySheetRows(
      rows,
      drafts,
      columns,
      source === 'online' && canAdd,
      index => `draft-${generation}-${index}`,
      100,
    )
    const workbookId = `query-${Date.now()}-${revision}`
    const sheetId = `sheet-${revision}`
    const { univer, univerAPI } = createQueryUniver(
      container,
      UniverSheetsCorePreset({
        container,
        header: false,
        toolbar: false,
        footer: false,
        formulaBar: true,
        contextMenu: false,
      }),
      UniverSheetsDataValidationPreset({
        showEditOnDropdown: false,
        showSearchOnDropdown: true,
      }),
    )

    const workbook = univerAPI.createWorkbook({
      id: workbookId,
      name: businessType,
      locale: LocaleType.ZH_CN,
      sheetOrder: [sheetId],
      sheets: {
        [sheetId]: {
          id: sheetId,
          name: businessType,
          rowCount: sheetRows.length + 1,
          columnCount: columns.length,
        },
      },
    })
    const worksheet = workbook.getActiveSheet()
    const initialValues = [
      columns,
      ...sheetRows.map(row => columns.map(column => normalizeCellValue(row.data[column]))),
    ]
    worksheet.getRange(0, 0, initialValues.length, columns.length).setValues(initialValues)
    worksheet.setFreeze({ startRow: 1, startColumn: 0, xSplit: 0, ySplit: 1 })
    worksheet.setRowHeight(0, 36)
    worksheet.setRowHeights(1, sheetRows.length, 32)
    columns.forEach((column, index) => worksheet.setColumnWidth(index, columnWidth(column)))

    const allRange = worksheet.getRange(0, 0, initialValues.length, columns.length)
    allRange.setWrap(true)
    allRange.setBorder(
      univerAPI.Enum.BorderType.ALL,
      univerAPI.Enum.BorderStyle.THIN,
      '#d8dee9',
    )
    const headerRange = worksheet.getRange(0, 0, 1, columns.length)
    headerRange.setBackgroundColor('#e8eef8')
    headerRange.setFontWeight('bold')
    headerRange.setFontColor('#172033')

    sheetRows.forEach((descriptor, rowIndex) => {
      const worksheetRow = rowIndex + 1
      if (descriptor.kind === 'data') {
        if (descriptor.data.__conflict) {
          worksheet.getRange(worksheetRow, 0, 1, columns.length).setBackgroundColor('#fff1f0')
        } else if (descriptor.data.__pending) {
          worksheet.getRange(worksheetRow, 0, 1, columns.length).setBackgroundColor('#fffbe6')
        }
      }
      columns.forEach((column, columnIndex) => {
        if (canEditQuerySheetCell(source, descriptor, column, canAdd)) {
          worksheet.getRange(worksheetRow, columnIndex).setBackgroundColor('#f0f7ff')
        }
      })
    })

    const metaByColumn = Object.fromEntries(columnMeta.map(meta => [meta.field, meta]))
    columns.forEach((column, columnIndex) => {
      const meta = metaByColumn[column]
      const options = meta?.type === 'select'
        ? (meta.options || []).map(option => option.text).filter(Boolean)
        : []
      if (!options.length) return
      const rule = univerAPI
        .newDataValidation()
        .requireValueInList(options, Boolean(meta.multiple), true)
        .setAllowBlank(true)
        .setAllowInvalid(true)
        .build()
      worksheet.getRange(1, columnIndex, sheetRows.length, 1).setDataValidation(rule)
    })

    let disposed = false
    let suppressCommands = false
    let saving = false
    let reconcileTimer: ReturnType<typeof setTimeout> | undefined
    let selectedWorksheetRow = -1

    const restoreChanges = (changes: QuerySheetCellChange[]) => {
      suppressCommands = true
      try {
        for (const change of changes) {
          const descriptorIndex = sheetRows.findIndex(item => item.data === change.row)
          const columnIndex = columns.indexOf(change.column)
          if (descriptorIndex < 0 || columnIndex < 0) continue
          change.row[change.column] = change.before
          worksheet.getRange(descriptorIndex + 1, columnIndex).setValue(change.before)
        }
      } finally {
        suppressCommands = false
      }
    }

    const reportSelection = () => {
      callbacksRef.current.onSelectionChange(
        selectedQuerySheetRow(sheetRows, selectedWorksheetRow),
      )
    }

    const reconcileValues = async () => {
      if (disposed || saving) return
      const dataRange = worksheet.getRange(1, 0, sheetRows.length, columns.length)
      const values = dataRange.getValues()
      const formulas = dataRange.getFormulas()
      const changes = applyQuerySheetValues(sheetRows, columns, values)
      if (!changes.length) return

      const blocked: QuerySheetCellChange[] = []
      const accepted: QuerySheetCellChange[] = []
      for (const change of changes) {
        const descriptorIndex = sheetRows.findIndex(item => item.data === change.row)
        const descriptor = sheetRows[descriptorIndex]
        const columnIndex = columns.indexOf(change.column)
        const isFormula = Boolean(formulas[descriptorIndex]?.[columnIndex])
        if (!isFormula && canEditQuerySheetCell(source, descriptor, change.column, canAdd)) accepted.push(change)
        else blocked.push(change)
      }
      if (blocked.length) {
        restoreChanges(blocked)
        callbacksRef.current.onBlocked('只读单元格或公式不能写回腾讯表格，相关修改已撤销')
      }
      if (!accepted.length) return

      callbacksRef.current.onDraftsChange(updateQuerySheetDrafts(sheetRows, columns))
      reportSelection()
      const sourceChanges = accepted.filter(change => change.row.__kind !== 'draft')
      if (!sourceChanges.length) return

      saving = true
      callbacksRef.current.onSavingChange?.(true)
      try {
        await callbacksRef.current.onCommit(sourceChanges)
      } catch {
        if (!disposed) restoreChanges(sourceChanges)
      } finally {
        saving = false
        callbacksRef.current.onSavingChange?.(false)
      }
    }

    const scheduleReconcile = () => {
      if (suppressCommands || disposed) return
      if (reconcileTimer) clearTimeout(reconcileTimer)
      reconcileTimer = setTimeout(() => { void reconcileValues() }, 60)
    }

    const rangesAreEditable = (ranges: IRange[]) => ranges.length > 0 && ranges.every(range => {
      const size = rangeSize(range)
      return isQuerySheetRangeEditable(
        source,
        sheetRows,
        columns,
        range.startRow,
        range.startColumn,
        size.rows,
        size.columns,
        canAdd,
      )
    })

    const disposables = [
      univerAPI.addEvent(univerAPI.Event.SelectionChanged, params => {
        const selection = params.selections[0]
        selectedWorksheetRow = selection?.startRow ?? -1
        reportSelection()
      }),
      univerAPI.addEvent(univerAPI.Event.BeforeSheetEditStart, params => {
        const descriptor = sheetRows[params.row - 1]
        const column = columns[params.column]
        if (saving || !canEditQuerySheetCell(source, descriptor, column, canAdd)) {
          params.cancel = true
          callbacksRef.current.onBlocked(
            saving ? '上一项修改仍在写回，请稍候' : '这个单元格为只读；蓝色单元格才可编辑',
          )
        }
      }),
      univerAPI.addEvent(univerAPI.Event.BeforeClipboardPaste, params => {
        const active = worksheet.getActiveRange()?.getRange()
        if (typeof params.text !== 'string') {
          params.cancel = true
          callbacksRef.current.onBlocked('无法识别剪贴板内容，本次粘贴已取消')
          return
        }
        const pasted = parseQuerySheetClipboard(params.text || '')
        const rowCount = pasted.length
        const columnCount = Math.max(0, ...pasted.map(row => row.length))
        if (
          saving
          || !active
          || !isQuerySheetRangeEditable(
            source,
            sheetRows,
            columns,
            active.startRow,
            active.startColumn,
            rowCount,
            columnCount,
            canAdd,
          )
        ) {
          params.cancel = true
          callbacksRef.current.onBlocked('粘贴区域包含只读单元格，本次粘贴已取消')
        }
      }),
      univerAPI.addEvent(univerAPI.Event.BeforeCommandExecute, event => {
        if (suppressCommands) return
        if ([UndoCommand.id, RedoCommand.id].includes(event.id)) {
          event.cancel = true
          callbacksRef.current.onBlocked('在线工作表不提供本地撤销；保存失败时系统会自动恢复原值')
          return
        }
        if ([AutoFillCommand.id, MoveRangeCommand.id].includes(event.id)) {
          event.cancel = true
          callbacksRef.current.onBlocked('暂不支持拖动填充或移动单元格，请使用复制粘贴')
          return
        }
        if ([SetRangeValuesCommand.id, ClearSelectionContentCommand.id, ClearSelectionAllCommand.id].includes(event.id)) {
          const ranges = commandRanges(event.params, worksheet)
          if (saving || !rangesAreEditable(ranges)) {
            event.cancel = true
            callbacksRef.current.onBlocked(
              saving ? '上一项修改仍在写回，请稍候' : '所选区域包含只读单元格，操作已取消',
            )
          }
          return
        }
        if (/insert|remove|append-row|delete-range|move-row|move-col/i.test(event.id)) {
          event.cancel = true
          callbacksRef.current.onBlocked('在线查询页不允许改变工作表结构')
        }
      }),
      univerAPI.addEvent(univerAPI.Event.CommandExecuted, event => {
        if ([SetRangeValuesCommand.id, ClearSelectionContentCommand.id, ClearSelectionAllCommand.id].includes(event.id)) {
          scheduleReconcile()
        }
      }),
      univerAPI.addEvent(univerAPI.Event.SheetEditEnded, scheduleReconcile),
      univerAPI.addEvent(univerAPI.Event.ClipboardPasted, scheduleReconcile),
    ]

    return () => {
      disposed = true
      if (reconcileTimer) clearTimeout(reconcileTimer)
      disposables.forEach(disposable => disposable.dispose())
      univer.dispose()
      container.replaceChildren()
    }
  // `revision` is the explicit rebuild boundary. Draft edits update the parent state,
  // but must not destroy and recreate the workbook while the user is typing.
  }, [businessType, canAdd, columnMeta, columns, revision, source])

  return <div ref={containerRef} className="query-spreadsheet" aria-label={`${businessType}在线工作表`} />
}
