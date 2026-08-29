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
import { UniverSheetsSortPreset } from '@univerjs/preset-sheets-sort'
import sortZhCN from '@univerjs/preset-sheets-sort/locales/zh-CN'

import '@univerjs/preset-sheets-core/lib/index.css'
import '@univerjs/preset-sheets-data-validation/lib/index.css'
import '@univerjs/preset-sheets-filter/lib/index.css'
import '@univerjs/preset-sheets-sort/lib/index.css'

import type { QueryColumnMeta, QueryDataRow, QueryDependentOptions } from '../api/client'
import { useAppThemeMode } from './AppThemeProvider'
import type { QueryDisplayRow } from '../utils/queryGrid'
import {
  applyQuerySheetValues,
  buildQuerySheetRows,
  canEditQuerySheetCell,
  createQuerySheetClipboardSnapshot,
  isQuerySheetAutomaticTextConversion,
  isQuerySheetRangeEditable,
  QUERY_SHEET_FEATURE_CONFIG,
  QUERY_SHEET_UI_CONFIG,
  querySheetPalette,
  queryInspectorMismatch,
  queryInspectorOptions,
  querySheetTextCell,
  querySheetCellKey,
  resolveQuerySheetColumnWidth,
  resolveQuerySheetPasteValues,
  resolveQuerySheetSortRequest,
  resolveQuerySheetThinBorderStyle,
  selectedQuerySheetRow,
  updateQuerySheetDrafts,
  type QuerySheetCellChange,
  type QuerySheetClipboardSnapshot,
  type QuerySheetFilterCriteria,
  type QuerySheetRow,
} from '../utils/querySpreadsheet'

export interface QuerySpreadsheetProps {
  businessType: string
  source: 'online' | 'archive'
  rows: QueryDataRow[]
  columns: string[]
  columnMeta: QueryColumnMeta[]
  dependentOptions?: QueryDependentOptions
  drafts: QueryDisplayRow[]
  canAdd: boolean
  revision: number
  layoutRevision?: number
  filterCriteria: Record<string, QuerySheetFilterCriteria>
  onSortChange: (column: string, order: 'asc' | 'desc') => void
  onDraftsChange: (drafts: QueryDisplayRow[]) => void
  onFilterCriteriaChange: (criteria: Record<string, QuerySheetFilterCriteria>) => void
  onSelectionChange: (row: QueryDisplayRow | null) => void
  onCommit: (changes: QuerySheetCellChange[]) => Promise<void>
  onBlocked: (message: string) => void
  onSavingChange?: (saving: boolean) => void
  onEditingChange?: (editing: boolean) => void
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
  sortPreset: ReturnType<typeof UniverSheetsSortPreset>,
) {
  const univer = new Univer({
    locale: LocaleType.ZH_CN,
    locales: {
      [LocaleType.ZH_CN]: merge({}, sheetsZhCN, validationZhCN, filterZhCN, sortZhCN),
    },
    theme: defaultTheme,
    logLevel: LogLevel.WARN,
  })
  const plugins = new Map<string, { plugin: any; options?: any }>()
  for (const preset of [corePreset, validationPreset, filterPreset, sortPreset]) {
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
  dependentOptions,
  drafts,
  canAdd,
  revision,
  layoutRevision = 0,
  filterCriteria,
  onSortChange,
  onDraftsChange,
  onFilterCriteriaChange,
  onSelectionChange,
  onCommit,
  onBlocked,
  onSavingChange,
  onEditingChange,
}: QuerySpreadsheetProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const themeMode = useAppThemeMode()
  const themeModeRef = useRef(themeMode)
  const filterCriteriaRef = useRef(filterCriteria)
  const applyAppearanceRef = useRef<((darkMode: boolean) => void) | null>(null)
  const callbacksRef = useRef({
    onDraftsChange,
    onFilterCriteriaChange,
    onSortChange,
    onSelectionChange,
    onCommit,
    onBlocked,
    onSavingChange,
    onEditingChange,
  })

  callbacksRef.current = {
    onDraftsChange,
    onFilterCriteriaChange,
    onSortChange,
    onSelectionChange,
    onCommit,
    onBlocked,
    onSavingChange,
    onEditingChange,
  }
  themeModeRef.current = themeMode
  filterCriteriaRef.current = filterCriteria

  useEffect(() => {
    const container = containerRef.current
    const pageScroller = container?.closest('main')
    const documentScroller = document.scrollingElement
    if (!container || !(pageScroller instanceof HTMLElement) || !documentScroller) return

    let lockedPosition: {
      mainLeft: number
      mainTop: number
      documentLeft: number
      documentTop: number
    } | null = null
    let releaseFrame = 0
    let releaseTimer = 0

    function clearReleaseSchedule() {
      if (releaseFrame) {
        window.cancelAnimationFrame(releaseFrame)
        releaseFrame = 0
      }
      if (releaseTimer) {
        window.clearTimeout(releaseTimer)
        releaseTimer = 0
      }
    }
    function restorePagePosition() {
      if (!lockedPosition) return
      if (pageScroller.scrollLeft !== lockedPosition.mainLeft) pageScroller.scrollLeft = lockedPosition.mainLeft
      if (pageScroller.scrollTop !== lockedPosition.mainTop) pageScroller.scrollTop = lockedPosition.mainTop
      if (documentScroller.scrollLeft !== lockedPosition.documentLeft) {
        documentScroller.scrollLeft = lockedPosition.documentLeft
      }
      if (documentScroller.scrollTop !== lockedPosition.documentTop) {
        documentScroller.scrollTop = lockedPosition.documentTop
      }
    }
    function unlock() {
      restorePagePosition()
      lockedPosition = null
      pageScroller.removeEventListener('scroll', restorePagePosition)
      documentScroller.removeEventListener('scroll', restorePagePosition)
      window.removeEventListener('scroll', restorePagePosition)
      window.removeEventListener('pointerup', finishGesture)
      window.removeEventListener('pointercancel', finishGesture)
    }
    function finishGesture() {
      window.removeEventListener('pointerup', finishGesture)
      window.removeEventListener('pointercancel', finishGesture)
      if (!lockedPosition) return
      restorePagePosition()
      releaseFrame = window.requestAnimationFrame(() => {
        releaseFrame = 0
        restorePagePosition()
        window.requestAnimationFrame(restorePagePosition)
      })
      // Univer may focus its hidden editor and recalculate the canvas after the
      // pointer is released. Keep the outer page fixed through that delayed work.
      releaseTimer = window.setTimeout(() => {
        releaseTimer = 0
        unlock()
      }, 420)
    }
    function handlePointerDown(event: PointerEvent) {
      if (!event.isPrimary || (event.pointerType === 'mouse' && event.button !== 0)) return
      clearReleaseSchedule()
      unlock()
      window.removeEventListener('pointerup', finishGesture)
      window.removeEventListener('pointercancel', finishGesture)
      lockedPosition = {
        mainLeft: pageScroller.scrollLeft,
        mainTop: pageScroller.scrollTop,
        documentLeft: documentScroller.scrollLeft,
        documentTop: documentScroller.scrollTop,
      }
      pageScroller.addEventListener('scroll', restorePagePosition)
      documentScroller.addEventListener('scroll', restorePagePosition)
      window.addEventListener('scroll', restorePagePosition)
      window.addEventListener('pointerup', finishGesture, { once: true })
      window.addEventListener('pointercancel', finishGesture, { once: true })
    }

    container.addEventListener('pointerdown', handlePointerDown, true)
    return () => {
      container.removeEventListener('pointerdown', handlePointerDown, true)
      clearReleaseSchedule()
      unlock()
    }
  }, [])

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
      UniverSheetsSortPreset(),
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
      columns.map(column => querySheetTextCell(column)),
      ...sheetRows.map(row => columns.map(column => querySheetTextCell(row.data[column], column))),
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

    const applyRowAppearance = (rowIndex: number, darkMode: boolean) => {
      const palette = querySheetPalette(darkMode)
      const descriptor = sheetRows[rowIndex]
      if (!descriptor) return
      const worksheetRow = rowIndex + 1
      const rowRange = worksheet.getRange(worksheetRow, 0, 1, columns.length)
      let rowBackground = palette.background
      if (descriptor.kind === 'data') {
        if (descriptor.data.__conflict) rowBackground = palette.conflict
        else if (descriptor.data.__pending) rowBackground = palette.pending
      }
      rowRange.setBackgroundColor(rowBackground)
      rowRange.setFontColor(palette.text)
      columns.forEach((column, columnIndex) => {
        if (canEditQuerySheetCell(source, descriptor, column, canAdd)) {
          worksheet.getRange(worksheetRow, columnIndex).setBackgroundColor(palette.editable)
        }
        if (
          dependentOptions
          && column === dependentOptions.inspector_column
          && queryInspectorMismatch(dependentOptions, descriptor.data)
        ) {
          worksheet.getRange(worksheetRow, columnIndex).setBackgroundColor(palette.warning)
        }
      })
    }

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
      sheetRows.forEach((_, rowIndex) => applyRowAppearance(rowIndex, darkMode))
    }
    applyAppearanceRef.current = applyAppearance
    applyAppearance(themeModeRef.current === 'dark')

    const metaByColumn = Object.fromEntries(columnMeta.map(meta => [meta.field, meta]))
    const buildValidationRule = (options: string[], multiple = false) => univerAPI
      .newDataValidation()
      .requireValueInList(options, multiple, true)
      .setOptions({
        renderMode: univerAPI.Enum.DataValidationRenderMode.ARROW,
      })
      .setAllowBlank(true)
      .setAllowInvalid(true)
      .build()
    columns.forEach((column, columnIndex) => {
      const meta = metaByColumn[column]
      if (dependentOptions && column === dependentOptions.inspector_column) return
      const options = meta?.type === 'select'
        ? (meta.options || []).map(option => option.text).filter(Boolean)
        : []
      if (!options.length) return
      const rule = buildValidationRule(options, Boolean(meta.multiple))
      worksheet.getRange(1, columnIndex, sheetRows.length, 1).setDataValidation(rule)
    })

    const inspectorColumnIndex = dependentOptions
      ? columns.indexOf(dependentOptions.inspector_column)
      : -1
    const applyInspectorValidation = (rowIndex: number) => {
      if (!dependentOptions || inspectorColumnIndex < 0) return
      const descriptor = sheetRows[rowIndex]
      if (!descriptor) return
      const options = queryInspectorOptions(dependentOptions, descriptor.data)
      descriptor.data.__inspector_mismatch = queryInspectorMismatch(
        dependentOptions,
        descriptor.data,
      )
      if (options.length) {
        worksheet
          .getRange(rowIndex + 1, inspectorColumnIndex)
          .setDataValidation(buildValidationRule(options))
      }
    }
    sheetRows.forEach((_, rowIndex) => applyInspectorValidation(rowIndex))

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
    const pendingEditedCells = new Set<string>()
    const explicitEditedValues = new Map<string, string>()
    const editingValues = new Map<string, string>()
    let internalClipboard: QuerySheetClipboardSnapshot | null = null
    let pendingPaste: { range: IRange; values: string[][] } | null = null

    const markEditedRange = (range: IRange, values?: string[][]) => {
      const rowCount = range.endRow - range.startRow + 1
      const columnCount = range.endColumn - range.startColumn + 1
      for (let rowOffset = 0; rowOffset < rowCount; rowOffset += 1) {
        for (let columnOffset = 0; columnOffset < columnCount; columnOffset += 1) {
          const row = range.startRow + rowOffset
          const column = range.startColumn + columnOffset
          if (row < 1 || column < 0) continue
          const key = querySheetCellKey(row, column)
          pendingEditedCells.add(key)
          const value = values?.[rowOffset]?.[columnOffset]
          if (value !== undefined) explicitEditedValues.set(key, value)
        }
      }
    }

    const richTextToPlainText = (value: unknown): string | undefined => {
      if (value && typeof (value as { toPlainText?: unknown }).toPlainText === 'function') {
        return (value as { toPlainText: () => string }).toPlainText()
      }
      if (typeof value === 'string') return value
      return undefined
    }

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
            .setValue(querySheetTextCell(change.before, change.column))
          applyInspectorValidation(descriptorIndex)
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
      if (!pendingEditedCells.size) return
      const editedCells = new Set(pendingEditedCells)
      const editedValues = new Map(explicitEditedValues)
      pendingEditedCells.clear()
      explicitEditedValues.clear()
      const values: unknown[][] = []
      const formulas: string[][] = []
      editedCells.forEach(key => {
        const [rowText, columnText] = key.split(':')
        const worksheetRow = Number(rowText)
        const rowOffset = worksheetRow - 1
        const columnIndex = Number(columnText)
        if (!Number.isInteger(rowOffset) || !Number.isInteger(columnIndex)) return
        const cell = worksheet.getRange(worksheetRow, columnIndex)
        values[rowOffset] ||= []
        formulas[rowOffset] ||= []
        values[rowOffset][columnIndex] = editedValues.has(key)
          ? editedValues.get(key)
          : cell.getValue()
        formulas[rowOffset][columnIndex] = cell.getFormula()
      })
      const automaticConversions: QuerySheetCellChange[] = []
      for (const key of editedCells) {
        const [rowText, columnText] = key.split(':')
        const rowOffset = Number(rowText) - 1
        const columnIndex = Number(columnText)
        const descriptor = sheetRows[rowOffset]
        const column = columns[columnIndex]
        if (!descriptor || !column) continue
        if (editedValues.has(key) && !/(日期|时间)/u.test(column)) continue
        const before = String(descriptor.data[column] ?? '')
        const after = String(values[rowOffset]?.[columnIndex] ?? '')
        if (!isQuerySheetAutomaticTextConversion(column, before, after)) continue
        automaticConversions.push({ row: descriptor.data, column, before, after })
        if (values[rowOffset]) values[rowOffset][columnIndex] = before
      }
      if (automaticConversions.length) {
        restoreChanges(automaticConversions)
        callbacksRef.current.onBlocked('检测到表格自动转换了长数字，已恢复原值且未保存；如需修改请重新明确输入完整文本')
        automaticConversions.forEach(change => {
          const descriptorIndex = sheetRows.findIndex(item => item.data === change.row)
          const columnIndex = columns.indexOf(change.column)
          if (descriptorIndex >= 0 && columnIndex >= 0) {
            editedCells.delete(querySheetCellKey(descriptorIndex + 1, columnIndex))
          }
        })
      }
      const changes = applyQuerySheetValues(sheetRows, columns, values, editedCells)
      changes.forEach(change => {
        const descriptorIndex = sheetRows.findIndex(item => item.data === change.row)
        const columnIndex = columns.indexOf(change.column)
        change.explicitTextEdit = descriptorIndex >= 0
          && columnIndex >= 0
          && editedValues.has(querySheetCellKey(descriptorIndex + 1, columnIndex))
      })
      if (!changes.length) return
      const changedRows = new Set(
        changes.map(change => sheetRows.findIndex(item => item.data === change.row)),
      )
      changedRows.forEach(rowIndex => applyInspectorValidation(rowIndex))
      changedRows.forEach(rowIndex => applyRowAppearance(rowIndex, themeModeRef.current === 'dark'))

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
        callbacksRef.current.onBlocked('只读单元格或公式不能保存，相关修改已撤销')
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
        changedRows.forEach(rowIndex => applyRowAppearance(rowIndex, themeModeRef.current === 'dark'))
      } catch {
        if (!disposed) {
          restoreChanges(sourceChanges)
          changedRows.forEach(rowIndex => applyRowAppearance(rowIndex, themeModeRef.current === 'dark'))
        }
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
      univerAPI.addEvent(univerAPI.Event.SheetBeforeRangeSort, params => {
        params.cancel = true
        const range = params.range.getRange()
        const sortRequest = resolveQuerySheetSortRequest(
          columns,
          range.startColumn,
          params.sortColumn,
        )
        if (!sortRequest) {
          callbacksRef.current.onBlocked('无法识别排序字段，请重新选择数据列')
          return
        }
        if (params.sortColumn.length > 1) {
          callbacksRef.current.onBlocked('在线查询暂时只支持单列排序，已按第一排序条件处理')
        }
        callbacksRef.current.onSortChange(sortRequest.column, sortRequest.order)
      }),
      univerAPI.addEvent(univerAPI.Event.BeforeSheetEditStart, params => {
        const descriptor = sheetRows[params.row - 1]
        const column = columns[params.column]
        if (saving || !canEditQuerySheetCell(source, descriptor, column, canAdd)) {
          params.cancel = true
          callbacksRef.current.onBlocked(
            saving ? '上一项修改仍在写回，请稍候' : '这个单元格为只读；蓝色单元格才可编辑',
          )
        } else if (column === dependentOptions?.inspector_column) {
          applyInspectorValidation(params.row - 1)
        }
        if (!params.cancel) callbacksRef.current.onEditingChange?.(true)
        editingValues.delete(querySheetCellKey(params.row, params.column))
      }),
      univerAPI.addEvent(univerAPI.Event.BeforeClipboardChange, params => {
        internalClipboard = createQuerySheetClipboardSnapshot(
          params.text,
          params.fromRange?.getValues?.(),
        )
      }),
      univerAPI.addEvent(univerAPI.Event.BeforeClipboardPaste, params => {
        const active = worksheet.getActiveRange()?.getRange()
        const pasted = resolveQuerySheetPasteValues(params.text, internalClipboard)
        if (!pasted) {
          params.cancel = true
          callbacksRef.current.onBlocked('无法识别剪贴板内容，本次粘贴已取消')
          return
        }
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
        } else if (active) {
          pendingPaste = {
            range: {
              startRow: active.startRow,
              startColumn: active.startColumn,
              endRow: active.startRow + rowCount - 1,
              endColumn: active.startColumn + columnCount - 1,
            },
            values: pasted,
          }
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
          } else {
            ranges.forEach(range => markEditedRange(
              range,
              event.id === ClearSelectionContentCommand.id || event.id === ClearSelectionAllCommand.id
                ? Array.from({ length: range.endRow - range.startRow + 1 }, () => (
                  Array.from({ length: range.endColumn - range.startColumn + 1 }, () => '')
                ))
                : undefined,
            ))
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
      univerAPI.addEvent(univerAPI.Event.SheetEditChanging, params => {
        const editValue = richTextToPlainText(params.value)
        if (editValue !== undefined && params.row >= 1 && params.column >= 0) {
          editingValues.set(querySheetCellKey(params.row, params.column), editValue)
        }
      }),
      univerAPI.addEvent(univerAPI.Event.BeforeSheetEditEnd, params => {
        if (params.isConfirm && params.row >= 1 && params.column >= 0) {
          const key = querySheetCellKey(params.row, params.column)
          const editValue = editingValues.get(key) ?? richTextToPlainText(params.value)
          markEditedRange(
            {
              startRow: params.row,
              startColumn: params.column,
              endRow: params.row,
              endColumn: params.column,
            },
            editValue === undefined ? undefined : [[editValue]],
          )
        }
      }),
      univerAPI.addEvent(univerAPI.Event.SheetEditEnded, params => {
        callbacksRef.current.onEditingChange?.(false)
        editingValues.delete(querySheetCellKey(params.row, params.column))
        if (!params.isConfirm) return
        if (params.row >= 1 && params.column >= 0) {
          markEditedRange({
            startRow: params.row,
            startColumn: params.column,
            endRow: params.row,
            endColumn: params.column,
          })
        }
        scheduleReconcile()
      }),
      univerAPI.addEvent(univerAPI.Event.ClipboardPasted, () => {
        if (pendingPaste) {
          markEditedRange(pendingPaste.range, pendingPaste.values)
          pendingPaste = null
        }
        scheduleReconcile()
      }),
    ]

    return () => {
      disposed = true
      callbacksRef.current.onEditingChange?.(false)
      if (reconcileTimer) clearTimeout(reconcileTimer)
      disposables.forEach(disposable => disposable.dispose())
      if (applyAppearanceRef.current === applyAppearance) applyAppearanceRef.current = null
      univer.dispose()
      container.replaceChildren()
    }
  // `revision` is the explicit rebuild boundary. Draft edits update the parent state,
  // but must not destroy and recreate the workbook while the user is typing.
  }, [businessType, canAdd, columnMeta, columns, dependentOptions, revision, source])

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
