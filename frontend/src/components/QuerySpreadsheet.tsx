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
import { UniverSheetsFilterPreset } from '@univerjs/preset-sheets-filter'
import filterZhCN from '@univerjs/preset-sheets-filter/locales/zh-CN'

import '@univerjs/preset-sheets-core/lib/index.css'
import '@univerjs/preset-sheets-data-validation/lib/index.css'
import '@univerjs/preset-sheets-filter/lib/index.css'

import type { QueryColumnMeta, QueryDataRow } from '../api/client'
import { useAppThemeMode } from './AppThemeProvider'
import type { QueryDisplayRow } from '../utils/queryGrid'
import {
  applyQuerySheetValues,
  buildQuerySheetRows,
  canEditQuerySheetCell,
  isQuerySheetRangeEditable,
  parseQuerySheetClipboard,
  QUERY_SHEET_FEATURE_CONFIG,
  QUERY_SHEET_UI_CONFIG,
  querySheetPalette,
  querySheetTextCell,
  resolveQuerySheetColumnWidth,
  resolveQuerySheetThinBorderStyle,
  selectedQuerySheetRow,
  updateQuerySheetDrafts,
  type QuerySheetCellChange,
  type QuerySheetFilterCriteria,
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
  layoutRevision?: number
  filterCriteria: Record<string, QuerySheetFilterCriteria>
  onDraftsChange: (drafts: QueryDisplayRow[]) => void
  onFilterCriteriaChange: (criteria: Record<string, QuerySheetFilterCriteria>) => void
  onSelectionChange: (row: QueryDisplayRow | null) => void
  onCommit: (changes: QuerySheetCellChange[]) => Promise<void>
  onBlocked: (message: string) => void
  onSavingChange?: (saving: boolean) => void
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

function createQueryUniver(
  container: HTMLElement,
  corePreset: ReturnType<typeof UniverSheetsCorePreset>,
  validationPreset: ReturnType<typeof UniverSheetsDataValidationPreset>,
  filterPreset: ReturnType<typeof UniverSheetsFilterPreset>,
) {
  const univer = new Univer({
    locale: LocaleType.ZH_CN,
    locales: {
      [LocaleType.ZH_CN]: merge({}, sheetsZhCN, validationZhCN, filterZhCN),
    },
    theme: defaultTheme,
    logLevel: LogLevel.WARN,
  })
  const plugins = new Map<string, { plugin: any; options?: any }>()
  for (const preset of [corePreset, validationPreset, filterPreset]) {
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
  layoutRevision = 0,
  filterCriteria,
  onDraftsChange,
  onFilterCriteriaChange,
  onSelectionChange,
  onCommit,
  onBlocked,
  onSavingChange,
}: QuerySpreadsheetProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const themeMode = useAppThemeMode()
  const themeModeRef = useRef(themeMode)
  const filterCriteriaRef = useRef(filterCriteria)
  const applyAppearanceRef = useRef<((darkMode: boolean) => void) | null>(null)
  const callbacksRef = useRef({
    onDraftsChange,
    onFilterCriteriaChange,
    onSelectionChange,
    onCommit,
    onBlocked,
    onSavingChange,
  })

  callbacksRef.current = {
    onDraftsChange,
    onFilterCriteriaChange,
    onSelectionChange,
    onCommit,
    onBlocked,
    onSavingChange,
  }
  themeModeRef.current = themeMode
  filterCriteriaRef.current = filterCriteria

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
        ...QUERY_SHEET_UI_CONFIG,
        sheets: QUERY_SHEET_FEATURE_CONFIG,
      }),
      UniverSheetsDataValidationPreset({
        showEditOnDropdown: false,
        showSearchOnDropdown: true,
      }),
      UniverSheetsFilterPreset(),
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
      columns.map(querySheetTextCell),
      ...sheetRows.map(row => columns.map(column => querySheetTextCell(row.data[column]))),
    ]
    worksheet.getRange(0, 0, initialValues.length, columns.length).setValues(initialValues)
    worksheet.setFreeze({ startRow: 1, startColumn: 0, xSplit: 0, ySplit: 1 })
    worksheet.setRowHeight(0, 36)
    worksheet.setRowHeights(1, sheetRows.length, 32)
    columns.forEach((column, index) => {
      worksheet.setColumnWidth(
        index,
        resolveQuerySheetColumnWidth(
          column,
          sheetRows.map(row => row.data[column]),
        ),
      )
    })

    const allRange = worksheet.getRange(0, 0, initialValues.length, columns.length)
    allRange.setWrap(true)
    const thinBorderStyle = resolveQuerySheetThinBorderStyle(univerAPI.Enum)
    const headerRange = worksheet.getRange(0, 0, 1, columns.length)
    headerRange.setFontWeight('bold')

    const applyAppearance = (darkMode: boolean) => {
      const palette = querySheetPalette(darkMode)
      univerAPI.toggleDarkMode(darkMode)
      allRange.setBackgroundColor(palette.background)
      allRange.setFontColor(palette.text)
      if (thinBorderStyle !== null) {
        allRange.setBorder(
          univerAPI.Enum.BorderType.ALL,
          thinBorderStyle,
          palette.border,
        )
      }
      headerRange.setBackgroundColor(palette.header)
      headerRange.setFontColor(palette.text)

      sheetRows.forEach((descriptor, rowIndex) => {
        const worksheetRow = rowIndex + 1
        if (descriptor.kind === 'data') {
          if (descriptor.data.__conflict) {
            worksheet.getRange(worksheetRow, 0, 1, columns.length).setBackgroundColor(palette.conflict)
          } else if (descriptor.data.__pending) {
            worksheet.getRange(worksheetRow, 0, 1, columns.length).setBackgroundColor(palette.pending)
          }
        }
        columns.forEach((column, columnIndex) => {
          if (canEditQuerySheetCell(source, descriptor, column, canAdd)) {
            worksheet.getRange(worksheetRow, columnIndex).setBackgroundColor(palette.editable)
          }
        })
      })
    }
    applyAppearanceRef.current = applyAppearance
    applyAppearance(themeModeRef.current === 'dark')

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

    const firstBlankRow = sheetRows.findIndex(row => row.kind === 'blank')
    const filterDataRowCount = firstBlankRow >= 0 ? firstBlankRow : sheetRows.length
    const sheetFilter = worksheet
      .getRange(0, 0, Math.max(1, filterDataRowCount + 1), columns.length)
      .createFilter()
    if (sheetFilter) {
      Object.entries(filterCriteriaRef.current).forEach(([column, criteria]) => {
        const columnIndex = columns.indexOf(column)
        if (columnIndex >= 0) {
          sheetFilter.setColumnFilterCriteria(columnIndex, {
            ...criteria,
            colId: columnIndex,
          })
        }
      })
    }

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
          worksheet
            .getRange(descriptorIndex + 1, columnIndex)
            .setValue(querySheetTextCell(change.before))
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
      univerAPI.addEvent(univerAPI.Event.SheetBeforeRangeFilter, params => {
        const colors = params.criteria?.colorFilters
        if (colors?.cellFillColors?.length || colors?.cellTextColors?.length) {
          params.cancel = true
          callbacksRef.current.onBlocked('在线查询暂不支持按颜色筛选，请改用值筛选或条件筛选')
        }
      }),
      univerAPI.addEvent(univerAPI.Event.SheetRangeFiltered, params => {
        const column = columns[params.col]
        if (!column) return
        const next = { ...filterCriteriaRef.current }
        if (params.criteria) {
          next[column] = JSON.parse(JSON.stringify(params.criteria)) as QuerySheetFilterCriteria
        } else {
          delete next[column]
        }
        filterCriteriaRef.current = next
        callbacksRef.current.onFilterCriteriaChange(next)
      }),
      univerAPI.addEvent(univerAPI.Event.SheetRangeFilterCleared, () => {
        filterCriteriaRef.current = {}
        callbacksRef.current.onFilterCriteriaChange({})
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
        if (/(insert|remove|append|delete|move).*(row|column|col|range)|(row|column|col|range).*(insert|remove|append|delete|move)/i.test(event.id)) {
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
      if (applyAppearanceRef.current === applyAppearance) applyAppearanceRef.current = null
      univer.dispose()
      container.replaceChildren()
    }
  // `revision` is the explicit rebuild boundary. Draft edits update the parent state,
  // but must not destroy and recreate the workbook while the user is typing.
  }, [businessType, canAdd, columnMeta, columns, revision, source])

  useEffect(() => {
    applyAppearanceRef.current?.(themeMode === 'dark')
  }, [themeMode])

  useEffect(() => {
    // Fullscreen changes the sheet container without changing the browser viewport.
    // Univer listens for window resize, so notify it after the fullscreen layout has
    // completed instead of rebuilding the workbook and losing the current selection.
    let secondFrame = 0
    const firstFrame = window.requestAnimationFrame(() => {
      secondFrame = window.requestAnimationFrame(() => {
        window.dispatchEvent(new Event('resize'))
      })
    })
    return () => {
      window.cancelAnimationFrame(firstFrame)
      if (secondFrame) window.cancelAnimationFrame(secondFrame)
    }
  }, [layoutRevision])

  return <div ref={containerRef} className="query-spreadsheet" aria-label={`${businessType}在线工作表`} />
}
