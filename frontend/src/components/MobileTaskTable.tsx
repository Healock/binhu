import AddressStatusTag from './AddressStatusTag'
import {
  CopyOutlined,
  ExclamationCircleOutlined,
} from '@ant-design/icons'
import { AutoComplete, Button, Input, Modal, Select, Table, Tag, Tooltip, message, type TableColumnsType } from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState, type Key } from 'react'
import {
  getMobileTaskInlineEditors,
  claimMobileTask,
  searchRegistrationProperties,
  updateMobileTask,
  updateMobileTaskAnalysis,
  type MobileTaskInlineEditorItem,
  type MobileTaskItem,
  type MobileTaskRegistrationProperty,
  type MobileTaskSort,
} from '../api/client'
import {
  buildMobileTaskChanges,
  formatMobileTaskDeadline,
  mergeMobileTaskSaveValues,
  mobileTaskEditorFields,
  mobileTaskCurrentAddressLabel,
  mobileTaskPhoneOptions,
  mobileTaskResultOptions,
  mobileTaskSourceTags,
  mobileTaskSurfaceTone,
  mobileTaskUsesRegistrationClosure,
} from '../utils/mobileTasks'
import { getCompactPersonnelPresentation } from '../utils/mobileTaskTableLayout'
import { useResponsiveLayout } from '../hooks/useResponsiveLayout'
import QmfFeedbackStatus from './QmfFeedbackStatus'
import ResidenceRegistrationStatus from './ResidenceRegistrationStatus'
import RegistrationLinkStatus from './RegistrationLinkStatus'
import UnverifiableReviewNotice from './UnverifiableReviewNotice'
import { getResponsiveColumns, type ResponsiveColumns } from './responsiveTable'

const STATE_LABELS = {
  unchecked: { text: '未核查', color: 'red' },
  checked: { text: '待补结果', color: 'orange' },
  completed: { text: '已完成', color: 'green' },
} as const

interface InlineRegistrationPropertyState {
  loading: boolean
  options: MobileTaskRegistrationProperty[]
  selectedId?: number
  matchStatus?: 'idle' | 'matching' | 'unique' | 'multiple' | 'none' | 'error'
}

function registrationPropertyAddress(property: MobileTaskRegistrationProperty) {
  return `${property.natural_address || ''}${property.building || ''}${property.room || ''}`.trim()
}

function registrationPropertyLabel(property: MobileTaskRegistrationProperty) {
  return registrationPropertyAddress(property)
}

interface MobileTaskTableProps {
  active?: boolean
  rows: MobileTaskItem[]
  loading: boolean
  analysisMode?: boolean
  canClaimUnassigned?: boolean
  selectionMode: boolean
  selectedRowKeys: Key[]
  canSelect: (task: MobileTaskItem) => boolean
  onSelect: (task: MobileTaskItem, selected: boolean) => void
  onOpen: (task: MobileTaskItem) => void
  onAddressOpen: (task: MobileTaskItem) => void
  onCopy: (value: string, label: '身份证号' | '手机号') => void
  sort: MobileTaskSort
  onSortChange: (sort: MobileTaskSort) => void
  filterOptions?: { community: { text: string; value: string }[]; smallCommunity: { text: string; value: string }[]; inspector: { text: string; value: string }[] }
  tableFilters?: { community: string[]; small_community: string[]; inspector: string[] }
  onTableFiltersChange?: (filters: Record<string, Key[] | null>) => void
}

function errorMessage(reason: any, fallback: string) {
  const detail = reason?.response?.data?.detail
  const value = typeof detail === 'object'
    ? detail?.message || fallback
    : detail || reason?.message || fallback
  if (typeof value === 'string') {
    const half = value.length / 2
    if (Number.isInteger(half) && value.slice(0, half) === value.slice(half)) return value.slice(0, half)
  }
  return value
}

export default function MobileTaskTable({
  active = true,
  rows,
  loading,
  analysisMode = false,
  canClaimUnassigned = false,
  selectionMode,
  selectedRowKeys,
  canSelect,
  onSelect,
  onOpen: onOpenTask,
  onAddressOpen,
  onCopy,
  sort,
  onSortChange,
  filterOptions,
  tableFilters,
  onTableFiltersChange,
}: MobileTaskTableProps) {
  const tableRef = useRef<HTMLDivElement>(null)
  const activeRef = useRef(active)
  activeRef.current = active
  const requestEpochRef = useRef(0)
  const previousRowsRef = useRef<Map<string, MobileTaskItem>>(new Map())
  const resumedRef = useRef(false)
  const responsiveLayout = useResponsiveLayout(tableRef)
  const compactPersonnelPresentation = getCompactPersonnelPresentation(responsiveLayout.width)
  const [editorItems, setEditorItems] = useState<Record<string, MobileTaskInlineEditorItem>>({})
  const [editorValues, setEditorValues] = useState<Record<string, Record<string, string>>>({})
  const editorValuesRef = useRef(editorValues)
  editorValuesRef.current = editorValues
  const [registrationProperties, setRegistrationProperties] = useState<Record<string, InlineRegistrationPropertyState>>({})
  const [loadingEditorKeys, setLoadingEditorKeys] = useState<Set<string>>(new Set())
  const [editorErrorKeys, setEditorErrorKeys] = useState<Set<string>>(new Set())
  const editorItemsRef = useRef<Record<string, MobileTaskInlineEditorItem>>({})
  const loadingEditorKeysRef = useRef<Set<string>>(new Set())
  const editorElementsRef = useRef<Map<string, HTMLElement>>(new Map())
  const editorObserverRef = useRef<IntersectionObserver | null>(null)
  const pendingEditorKeysRef = useRef<Set<string>>(new Set())
  const editorFlushTimerRef = useRef<number | null>(null)
  const claimPromptKeysRef = useRef<Set<string>>(new Set())
  const registrationSearchSequenceRef = useRef<Record<string, number>>({})
  const composingRef = useRef<Record<string, Record<string, boolean>>>({})
  const autosaveTimersRef = useRef<Record<string, number>>({})
  type FieldSaveState = 'idle' | 'composing' | 'saving' | 'saved' | 'error' | 'conflict'
  const [saveStates, setSaveStates] = useState<Record<string, Record<string, FieldSaveState>>>({})
  const stateRef = useRef<Record<string, Record<string, FieldSaveState>>>({})
  const draftVersionsRef = useRef<Record<string, Record<string, number>>>({})
  const lifecycleRef = useRef(0)
  const activeAutosavesRef = useRef<Set<symbol>>(new Set())
  const taskSaveChainsRef = useRef<Record<string, Promise<void>>>({})
  const retryRef = useRef<Record<string, () => void>>({})
  const conflictsRef = useRef<Record<string, { revision: number; values: Record<string, string> }>>({})
  const failedFieldsRef = useRef(new Set<string>())
  const cancelledClaimRef = useRef(new Set<string>())
  const chosenPropertyRef = useRef<Record<string, MobileTaskRegistrationProperty | undefined>>({})

  const fieldKey = (taskKey: string, field: string) => JSON.stringify([taskKey, field])
  const fieldState = (taskKey: string, field: string, state: FieldSaveState) => {
    const next = { ...stateRef.current, [taskKey]: { ...stateRef.current[taskKey], [field]: state } }
    stateRef.current = next
    setSaveStates(next)
  }
  const setDraft = (taskKey: string, field: string, value: string) => {
    const current = editorValuesRef.current[taskKey] || editorItemsRef.current[taskKey]?.detail?.sources[0]?.values || {}
    if (current[field] !== value) {
      draftVersionsRef.current[taskKey] = { ...draftVersionsRef.current[taskKey], [field]: (draftVersionsRef.current[taskKey]?.[field] || 0) + 1 }
    }
    const next = { ...current, [field]: value }
    editorValuesRef.current = { ...editorValuesRef.current, [taskKey]: next }
    setEditorValues(editorValuesRef.current)
    const state = stateRef.current[taskKey]?.[field]
    if (state !== 'conflict' && state !== 'error') fieldState(taskKey, field, composingRef.current[taskKey]?.[field] ? 'composing' : 'idle')
  }
  const taskByKey = useMemo(
    () => new Map(rows.map(task => [task.task_key, task])),
    [rows],
  )
  const parserTypesKey = useMemo(
    () => [...new Set(rows.map(task => task.parser_type))].sort().join('|'),
    [rows],
  )
  const editorContext = `${analysisMode ? 'analysis' : 'tasks'}:${parserTypesKey}`
  const editorContextRef = useRef(editorContext)

  const requestEditors = useCallback(async (taskKeys: string[], force = false) => {
    if (!activeRef.current) return
    const epoch = requestEpochRef.current
    const keys = [...new Set(taskKeys)].filter(taskKey => (
      taskKey
      && !loadingEditorKeysRef.current.has(taskKey)
      && (force || !editorItemsRef.current[taskKey])
    ))
    if (!keys.length) return
    const requestContext = editorContext
    keys.forEach(taskKey => loadingEditorKeysRef.current.add(taskKey))
    setLoadingEditorKeys(new Set(loadingEditorKeysRef.current))
    try {
      const grouped = keys.reduce<Record<string, Array<{ taskKey: string; rowKey: string }>>>((result, taskKey) => {
        const task = taskByKey.get(taskKey)
        if (task) (result[task.parser_type] ||= []).push({ taskKey, rowKey: task.row_key })
        return result
      }, {})
      const batches = Object.entries(grouped)
      const results = await Promise.all(
        batches.map(([parserType, parserTasks]) => (
          getMobileTaskInlineEditors(parserType, parserTasks.map(task => task.rowKey), analysisMode)
        )),
      )
      if (!activeRef.current || epoch !== requestEpochRef.current || requestContext !== editorContextRef.current) return
      const values: Record<string, Record<string, string>> = {}
      const items: Record<string, MobileTaskInlineEditorItem> = {}
      batches.forEach(([, parserTasks], batchIndex) => {
        parserTasks.forEach(task => {
          const item = results[batchIndex].items[task.rowKey]
          if (item) items[task.taskKey] = item
        })
      })
      Object.entries(items).forEach(([taskKey, item]) => {
        const source = item.detail?.sources[0]
        const oldItem = editorItemsRef.current[taskKey]
        const oldSource = oldItem?.detail?.sources[0]
        if (oldItem && source && oldSource && source.id === oldSource.id && source.revision < oldSource.revision) {
          items[taskKey] = oldItem
          return
        }
        const draft = editorValuesRef.current[taskKey]
        const dirty = oldSource && draft && (Object.values(composingRef.current[taskKey] || {}).some(Boolean)
          || Object.entries(draft).some(([field, value]) => String(oldSource.values[field] || '') !== String(value || '')))
        if (dirty) {
          // Do not silently rebase a pending write onto a newer server revision.
          // Keep the original base so background refresh cannot silently approve overwrites.
          if (source && source.revision > oldSource.revision) {
            for (const [field, value] of Object.entries(draft)) {
              if (value === oldSource.values[field] || source.values[field] === oldSource.values[field]) continue
              const key = fieldKey(taskKey, field)
              failedFieldsRef.current.add(key)
              conflictsRef.current[key] = { revision: source.revision, values: { [field]: source.values[field] || '' } }
              if (!composingRef.current[taskKey]?.[field]) fieldState(taskKey, field, 'conflict')
            }
          }
          if (item.available && item.detail && oldSource && source && source.id === oldSource.id) {
            items[taskKey] = { ...item, detail: { ...item.detail, sources: [{ ...oldSource, editable_fields: source.editable_fields }] } }
          }
        } else if (source) values[taskKey] = { ...source.values }
        if (oldItem?.detail && !item.available) items[taskKey] = { ...oldItem, reason: item.reason || '该任务已不可编辑，请返回列表核对', detail: { ...oldItem.detail, writeback_enabled: false } }
      })
      editorItemsRef.current = { ...editorItemsRef.current, ...items }
      setEditorItems(editorItemsRef.current)
      editorValuesRef.current = { ...editorValuesRef.current, ...values }
      setEditorValues(editorValuesRef.current)
      setEditorErrorKeys(current => new Set([...current].filter(key => !keys.includes(key))))
    } catch (reason: any) {
      if (activeRef.current && epoch === requestEpochRef.current && requestContext === editorContextRef.current) {
        setEditorErrorKeys(current => new Set([...current, ...keys]))
        message.error({
          key: 'mobile-task-inline-editor-load',
          content: errorMessage(reason, '当前可见任务的可编辑信息读取失败'),
        })
      }
    } finally {
      if (epoch === requestEpochRef.current && requestContext === editorContextRef.current) {
        keys.forEach(taskKey => loadingEditorKeysRef.current.delete(taskKey))
        setLoadingEditorKeys(new Set(loadingEditorKeysRef.current))
      }
    }
  }, [analysisMode, editorContext, taskByKey])

  const queueEditorLoad = useCallback((taskKey: string) => {
    if (
      !activeRef.current || !taskKey
      || editorItemsRef.current[taskKey]
      || loadingEditorKeysRef.current.has(taskKey)
      || pendingEditorKeysRef.current.has(taskKey)
    ) return
    pendingEditorKeysRef.current.add(taskKey)
    if (editorFlushTimerRef.current !== null) return
    editorFlushTimerRef.current = window.setTimeout(() => {
      editorFlushTimerRef.current = null
      const keys = [...pendingEditorKeysRef.current]
      pendingEditorKeysRef.current.clear()
      void requestEditors(keys)
    }, 60)
  }, [requestEditors])

  const setEditorElement = useCallback((taskKey: string, element: HTMLElement | null) => {
    const previous = editorElementsRef.current.get(taskKey)
    if (previous && previous !== element) editorObserverRef.current?.unobserve(previous)
    if (!element) {
      editorElementsRef.current.delete(taskKey)
      return
    }
    element.dataset.mobileTaskEditorRowKey = taskKey
    editorElementsRef.current.set(taskKey, element)
    editorObserverRef.current?.observe(element)
  }, [])

  useEffect(() => {
    editorContextRef.current = editorContext
    editorItemsRef.current = {}
    loadingEditorKeysRef.current.clear()
    pendingEditorKeysRef.current.clear()
    if (editorFlushTimerRef.current !== null) {
      window.clearTimeout(editorFlushTimerRef.current)
      editorFlushTimerRef.current = null
    }
    setEditorItems({})
    setEditorValues({})
    editorValuesRef.current = {}
    composingRef.current = {}
    stateRef.current = {}
    setSaveStates({})
    draftVersionsRef.current = {}
    failedFieldsRef.current.clear()
    conflictsRef.current = {}
    retryRef.current = {}
    setEditorErrorKeys(new Set())
    setRegistrationProperties({})
    registrationSearchSequenceRef.current = {}
    setLoadingEditorKeys(new Set())
    return () => {
      pendingEditorKeysRef.current.clear()
      if (editorFlushTimerRef.current !== null) {
        window.clearTimeout(editorFlushTimerRef.current)
        editorFlushTimerRef.current = null
      }
    }
  }, [editorContext])

  useEffect(() => {
    if (!active) {
      resumedRef.current = true
      ++requestEpochRef.current
      loadingEditorKeysRef.current.clear()
      setLoadingEditorKeys(new Set())
      pendingEditorKeysRef.current.clear()
      if (editorFlushTimerRef.current !== null) window.clearTimeout(editorFlushTimerRef.current)
      editorFlushTimerRef.current = null
      return
    }
    const changed = rows.filter(task => editorItemsRef.current[task.task_key]
      && (resumedRef.current || previousRowsRef.current.get(task.task_key) !== task))
    previousRowsRef.current = new Map(rows.map(task => [task.task_key, task]))
    resumedRef.current = false
    if (changed.length) void requestEditors(changed.map(task => task.task_key), true)
  }, [active, rows, requestEditors])

  useEffect(() => {
    if (!active) return undefined
    if (!parserTypesKey) return undefined
    if (typeof IntersectionObserver === 'undefined') {
      rows.slice(0, 20).forEach(task => queueEditorLoad(task.task_key))
      return undefined
    }
    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return
        const rowKey = (entry.target as HTMLElement).dataset.mobileTaskEditorRowKey
        if (rowKey) queueEditorLoad(rowKey)
      })
    }, { rootMargin: '600px 0px' })
    editorObserverRef.current = observer
    editorElementsRef.current.forEach(element => observer.observe(element))
    return () => {
      observer.disconnect()
      if (editorObserverRef.current === observer) editorObserverRef.current = null
    }
  }, [active, parserTypesKey, queueEditorLoad])

  const saveEditor = async (
    task: MobileTaskItem,
    item: MobileTaskInlineEditorItem,
    changes: Record<string, string>,
    claim = false,
    registrationProperty?: { id: number; version: number },
    options?: { fields: string[]; registrationAddress?: string },
  ): Promise<boolean> => {
    const detail = item.detail
    const source = detail?.sources[0]
    if (!activeRef.current || !item.available || !source || !detail?.writeback_enabled) return false
    const fields = options?.fields || Object.keys(changes)
    const epoch = lifecycleRef.current
    const sentDraft = { ...editorValuesRef.current[task.task_key] }
    const versions = { ...draftVersionsRef.current[task.task_key] }
    fields.forEach(field => fieldState(task.task_key, field, 'saving'))
    try {
      const updater = claim ? claimMobileTask : analysisMode ? updateMobileTaskAnalysis : updateMobileTask
      const result = await updater(task.parser_type, source.id, {
        changes,
        base_values: Object.fromEntries(Object.keys(changes).map(field => [field, source.values[field] || ''])),
        expected_revision: source.revision,
        ...(options?.registrationAddress !== undefined ? { registration_pending_address: options.registrationAddress } : {}),
        ...(registrationProperty ? { registration_property_id: registrationProperty.id, registration_property_version: registrationProperty.version } : {}),
      })
      if (epoch !== lifecycleRef.current) return false
      const latestItem = editorItemsRef.current[task.task_key] || item
      const latestSource = latestItem.detail?.sources[0]
      const savedValues = mergeMobileTaskSaveValues(source.values, changes, result.values, source.cell_meta)
      // A later editor refresh must never be replaced by an older save response.
      const newerServer = latestSource && latestSource.revision > result.revision
      const baseline = newerServer ? latestSource.values : savedValues
      if (!newerServer && latestItem.detail) {
        editorItemsRef.current = { ...editorItemsRef.current, [task.task_key]: {
          ...latestItem, detail: { ...latestItem.detail, sources: [{ ...(latestSource || source), values: savedValues, revision: result.revision }] },
        } }
        setEditorItems(editorItemsRef.current)
      }
      const draft = editorValuesRef.current[task.task_key] || sentDraft
      const next = { ...baseline }
      for (const [field, value] of Object.entries(draft)) {
        const editedSinceSend = (draftVersionsRef.current[task.task_key]?.[field] || 0) !== (versions[field] || 0)
        const unsent = !(field in changes) && sentDraft[field] !== source.values[field]
        if (editedSinceSend || unsent || composingRef.current[task.task_key]?.[field]) next[field] = value
        const expectedServerValue = field in changes ? savedValues[field] : source.values[field]
        if ((editedSinceSend || unsent) && baseline[field] !== expectedServerValue && value !== baseline[field]) {
          // A full-row response may contain another user's edits to an unsent
          // dirty field. Updating the row revision cannot authorize overwriting it.
          const key = fieldKey(task.task_key, field)
          failedFieldsRef.current.add(key)
          conflictsRef.current[key] = { revision: newerServer ? latestSource.revision : result.revision, values: { [field]: baseline[field] || '' } }
          if (!composingRef.current[task.task_key]?.[field]) fieldState(task.task_key, field, 'conflict')
        }
      }
      editorValuesRef.current = { ...editorValuesRef.current, [task.task_key]: next }
      setEditorValues(editorValuesRef.current)
      fields.forEach(field => {
        const key = fieldKey(task.task_key, field)
        const conflict = conflictsRef.current[key]
        // A refresh can observe this very save before its response reaches us.
        // That acknowledgement is not an external conflict with our next draft.
        if (!conflict || conflict.values[field] === savedValues[field]) {
          delete conflictsRef.current[key]
          failedFieldsRef.current.delete(key)
        }
        if ((draftVersionsRef.current[task.task_key]?.[field] || 0) !== (versions[field] || 0) || composingRef.current[task.task_key]?.[field]) return
        if (conflictsRef.current[key]) { fieldState(task.task_key, field, 'conflict'); return }
        fieldState(task.task_key, field, next[field] === baseline[field] ? 'saved' : 'idle')
      })
      return true
    } catch (reason: any) {
      if (epoch !== lifecycleRef.current) return false
      const info = reason?.response?.data?.detail
      const conflict = Number(reason?.response?.status) === 409 && info?.code === 'task_revision_conflict'
      const columns: string[] = conflict && Array.isArray(info.columns) ? info.columns : fields
      fields.forEach(field => {
        // Store retry barriers even when the user has typed ahead; do not repaint
        // their active composition with the state of an older request.
        if (conflict && columns.includes(field)) {
          conflictsRef.current[fieldKey(task.task_key, field)] = { revision: Number(info.current_revision), values: info.current_values || {} }
        }
        if ((draftVersionsRef.current[task.task_key]?.[field] || 0) !== (versions[field] || 0) || composingRef.current[task.task_key]?.[field]) return
        fieldState(task.task_key, field, conflict && columns.includes(field) ? 'conflict' : 'error')
      })
      // Pause automatic replay after every failure, including an obsolete response.
      fields.forEach(field => { failedFieldsRef.current.add(fieldKey(task.task_key, field)) })
      return false
    }
  }

  const confirmClaim = async (task: MobileTaskItem, sourceValues: Record<string, string>) => {
    if (cancelledClaimRef.current.has(task.task_key)) return null
    const shouldClaim = canClaimUnassigned
      && !analysisMode
      && !String(sourceValues['核查人'] || task.inspector || '').trim()
    if (!shouldClaim) return false
    if (claimPromptKeysRef.current.has(task.task_key)) return null
    claimPromptKeysRef.current.add(task.task_key)
    const confirmed = await new Promise<boolean>(resolve => {
      let settled = false
      const finish = (value: boolean) => {
        if (settled) return
        settled = true
        resolve(value)
      }
      Modal.confirm({
        title: '该任务暂未分配核查人，是否领取任务？',
        okText: '领取并保存',
        cancelText: '取消',
        onOk: () => finish(true),
        onCancel: () => finish(false),
        afterClose: () => finish(false),
      })
    })
    claimPromptKeysRef.current.delete(task.task_key)
    if (!confirmed) cancelledClaimRef.current.add(task.task_key)
    return confirmed ? true : null
  }

  const enqueueTaskSave = (taskKey: string, operation: () => Promise<boolean>) => {
    const previous = taskSaveChainsRef.current[taskKey] || Promise.resolve()
    const run = previous.catch(() => undefined).then(operation)
    taskSaveChainsRef.current[taskKey] = run.then(() => undefined, () => undefined)
    return run
  }

  const cancelScheduledFieldSave = (taskKey: string, field: string) => {
    const key = fieldKey(taskKey, field)
    window.clearTimeout(autosaveTimersRef.current[key])
    delete autosaveTimersRef.current[key]
  }

  const saveField = async (
    task: MobileTaskItem, item: MobileTaskInlineEditorItem, field: string, value: string, _autosave = true,
  ) => {
    const key = fieldKey(task.task_key, field)
    const epoch = lifecycleRef.current
    retryRef.current[key] = () => { void saveField(task, item, field, editorValuesRef.current[task.task_key]?.[field] ?? value) }
    const operationId = Symbol(key)
    activeAutosavesRef.current.add(operationId)
    try {
      await enqueueTaskSave(task.task_key, async () => {
        if (!activeRef.current || epoch !== lifecycleRef.current || composingRef.current[task.task_key]?.[field]) return false
        if (failedFieldsRef.current.has(key)) {
          fieldState(task.task_key, field, conflictsRef.current[key] ? 'conflict' : 'error')
          return false
        }
        let currentItem = editorItemsRef.current[task.task_key] || item
        let source = currentItem.detail?.sources[0]
        if (!source || !currentItem.available || !currentItem.detail?.writeback_enabled || !source.editable_fields.includes(field)) return false
        if (editorValuesRef.current[task.task_key]?.[field] === source.values[field] && !chosenPropertyRef.current[task.task_key]) return true
        const claim = await confirmClaim(task, source.values)
        if (epoch !== lifecycleRef.current || !activeRef.current || composingRef.current[task.task_key]?.[field]) return false
        if (claim === null) { fieldState(task.task_key, field, 'error'); failedFieldsRef.current.add(key); return false }
        // Read version, permissions and every value at dispatch time, after the queue and confirmation.
        currentItem = editorItemsRef.current[task.task_key] || item
        source = currentItem.detail?.sources[0]
        if (!source || !currentItem.available || !currentItem.detail?.writeback_enabled || !source.editable_fields.includes(field)) return false
        const draft = editorValuesRef.current[task.task_key] || source.values
        const resultField = currentItem.detail.workflow.result_field
        const registration = mobileTaskUsesRegistrationClosure(task.parser_type)
          && [resultField, '现住址'].includes(field) && draft[resultField] === '待登记'
        const fields = registration ? [resultField, '现住址'] : [field]
        if (fields.some(f => composingRef.current[task.task_key]?.[f])) return false
        if (fields.some(f => failedFieldsRef.current.has(fieldKey(task.task_key, f)))) return false
        const changes = buildMobileTaskChanges(source.values, draft, fields)
        const property = registration ? chosenPropertyRef.current[task.task_key] : undefined
        if (!Object.keys(changes).length && !property) { fieldState(task.task_key, field, 'saved'); return true }
        if (registration) {
          // Even an unchanged address needs registration context in this atomic request.
          changes[resultField] = '待登记'
          changes['现住址'] = draft['现住址'] || ''
          // An incomplete address is a local draft, not a failed request. Filling
          // it later must be able to complete the same atomic save automatically.
          if (!changes['现住址'].trim()) { fieldState(task.task_key, field, 'idle'); return false }
        } else if (!Object.keys(changes).length) { fieldState(task.task_key, field, 'saved'); return true }
        fields.forEach(f => { retryRef.current[fieldKey(task.task_key, f)] = () => { void saveField(task, item, f, editorValuesRef.current[task.task_key]?.[f] || '') } })
        const saved = await saveEditor(task, currentItem, changes, claim, property ? { id: property.id, version: property.version } : undefined, {
          fields, ...(registration && !property ? { registrationAddress: draft['现住址'] || '' } : {}),
        })
        if (saved && property === chosenPropertyRef.current[task.task_key]) delete chosenPropertyRef.current[task.task_key]
        return saved
      })
    } finally { activeAutosavesRef.current.delete(operationId) }
  }

  const scheduleFieldSave = (task: MobileTaskItem, item: MobileTaskInlineEditorItem, field: string, value: string) => {
    cancelScheduledFieldSave(task.task_key, field)
    if (composingRef.current[task.task_key]?.[field]) return
    autosaveTimersRef.current[fieldKey(task.task_key, field)] = window.setTimeout(() => {
      cancelScheduledFieldSave(task.task_key, field)
      void saveField(task, item, field, editorValuesRef.current[task.task_key]?.[field] ?? value)
    }, 700)
  }

  const retryAutosave = (taskKey: string, field: string) => {
    const key = fieldKey(taskKey, field)
    const conflict = conflictsRef.current[key]
    const retry = () => {
      cancelledClaimRef.current.delete(taskKey)
      // Companion fields of an atomic registration are retried together, but an
      // independently conflicting field still requires its own explicit review.
      const resultField = editorItemsRef.current[taskKey]?.detail?.workflow.result_field
      if (field === resultField || field === '现住址') {
        for (const f of [resultField, '现住址']) if (f && !conflictsRef.current[fieldKey(taskKey, f)]) failedFieldsRef.current.delete(fieldKey(taskKey, f))
      }
      failedFieldsRef.current.delete(key)
      if (conflict) {
        const item = editorItemsRef.current[taskKey]
        const source = item?.detail?.sources[0]
        if (item?.detail && source) {
          editorItemsRef.current = { ...editorItemsRef.current, [taskKey]: { ...item, detail: { ...item.detail,
            sources: [{ ...source, revision: Math.max(source.revision, conflict.revision), values: { ...source.values, [field]: conflict.values[field] ?? source.values[field] } }],
          } } }
          setEditorItems(editorItemsRef.current)
        }
        delete conflictsRef.current[key]
      }
      fieldState(taskKey, field, 'idle')
      retryRef.current[key]?.()
    }
    if (!conflict) { retry(); return }
    Modal.confirm({
      title: `核对${field}`,
      content: <div className="grid gap-3"><div>服务器当前值：{conflict.values[field] || '（空白）'}</div><div>本地草稿：{editorValuesRef.current[taskKey]?.[field] || '（空白）'}</div><div>确认后仅重试此字段；若服务器再次变化，仍会拒绝覆盖。</div></div>,
      okText: '保留草稿并重试', cancelText: '继续编辑', onOk: retry,
    })
  }

  useEffect(() => () => {
    ++lifecycleRef.current
    Object.values(autosaveTimersRef.current).forEach(timer => window.clearTimeout(timer))
    Object.entries(registrationSearchSequenceRef.current).filter(([key]) => key.endsWith(':timer')).forEach(([, timer]) => window.clearTimeout(timer))
    autosaveTimersRef.current = {}
  }, [editorContext])

  const compositionStart = (taskKey: string, field: string) => {
    composingRef.current[taskKey] = { ...composingRef.current[taskKey], [field]: true }
    cancelScheduledFieldSave(taskKey, field)
    window.clearTimeout(registrationSearchSequenceRef.current[`${taskKey}:timer`])
    fieldState(taskKey, field, 'composing')
  }
  const compositionEnd = (task: MobileTaskItem, item: MobileTaskInlineEditorItem, field: string, value: string) => {
    composingRef.current[task.task_key] = { ...composingRef.current[task.task_key], [field]: false }
    setDraft(task.task_key, field, value)
    scheduleFieldSave(task, item, field, value)
    if (field === '现住址') {
      window.clearTimeout(registrationSearchSequenceRef.current[`${task.task_key}:timer`])
      registrationSearchSequenceRef.current[`${task.task_key}:timer`] = window.setTimeout(() => void searchRegistrationProperty(task, editorValuesRef.current[task.task_key]?.[field] || ''), 700)
    }
  }
  const searchRegistrationProperty = async (task: MobileTaskItem, keyword: string) => {
    const epoch = lifecycleRef.current
    const normalized = keyword.trim()
    const sequence = (registrationSearchSequenceRef.current[task.task_key] || 0) + 1
    registrationSearchSequenceRef.current[task.task_key] = sequence
    if (!normalized) {
      setRegistrationProperties(current => ({
        ...current,
        [task.task_key]: { ...(current[task.task_key] || {}), loading: false, options: [] },
      }))
      return
    }
    setRegistrationProperties(current => ({
      ...current,
      [task.task_key]: { ...(current[task.task_key] || { options: [] }), loading: true },
    }))
    try {
      const result = await searchRegistrationProperties(normalized, task.community)
      if (epoch !== lifecycleRef.current || registrationSearchSequenceRef.current[task.task_key] !== sequence) return
      setRegistrationProperties(current => ({
        ...current,
        [task.task_key]: {
          ...(current[task.task_key] || {}),
          loading: false,
          options: result.data || [],
          matchStatus: (result.data || []).length === 1 ? 'unique' : (result.data || []).length > 1 ? 'multiple' : 'none',
          selectedId: current[task.task_key]?.selectedId,
        },
      }))
    } catch {
      if (epoch !== lifecycleRef.current || registrationSearchSequenceRef.current[task.task_key] !== sequence) return
      setRegistrationProperties(current => ({
        ...current,
          [task.task_key]: { ...(current[task.task_key] || {}), loading: false, options: [], matchStatus: 'error' },
      }))

    }
  }

  const saveRegistrationProperty = async (task: MobileTaskItem, item: MobileTaskInlineEditorItem, property: MobileTaskRegistrationProperty) => {
    const resultField = item.detail?.workflow.result_field
    const address = registrationPropertyAddress(property)
    if (!resultField || !address) return
    cancelScheduledFieldSave(task.task_key, '现住址')
    cancelScheduledFieldSave(task.task_key, resultField)
    chosenPropertyRef.current[task.task_key] = property
    setDraft(task.task_key, resultField, '待登记')
    setDraft(task.task_key, '现住址', address)
    await saveField(task, item, '现住址', address)
  }

  const flushAddressNavigation = async () => {
    const deadline = Date.now() + 15000
    while ((Object.keys(autosaveTimersRef.current).length || activeAutosavesRef.current.size) && Date.now() < deadline) {
      await new Promise(resolve => window.setTimeout(resolve, 100))
    }
    if (activeAutosavesRef.current.size || Object.keys(autosaveTimersRef.current).length) {
      message.warning('仍在保存，请稍后再打开确认地址'); return false
    }
    const hasDraft = Object.entries(editorValuesRef.current).some(([key, values]) => {
      const source = editorItemsRef.current[key]?.detail?.sources[0]
      return source && Object.entries(values).some(([field, value]) => String(source.values[field] || '') !== String(value || ''))
    })
    return !hasDraft || window.confirm('仍有未保存的修改。取消可继续编辑或重试；确定将放弃这些修改并前往确认地址。')
  }

  const onOpen = (task: MobileTaskItem) => {
    if (activeAutosavesRef.current.size || Object.keys(autosaveTimersRef.current).length) {
      message.info('正在保存填写内容，请保存结束后再打开详情。')
      return
    }
    const hasDraft = Object.entries(editorValuesRef.current).some(([key, values]) => {
      const source = editorItemsRef.current[key]?.detail?.sources[0]
      return source && Object.entries(values).some(([field, value]) => String(source.values[field] || '') !== String(value || ''))
    })
    if (hasDraft && !window.confirm('仍有未保存的修改。继续将保留列表草稿并打开详情，返回后可继续核对和重试。')) return
    onOpenTask(task)
  }

  const renderExpandedRow = (task: MobileTaskItem) => {
    const surfaceTone = mobileTaskSurfaceTone(task)
    const toneClass = `mobile-task-table-inline-editor--tone-${surfaceTone}`
    const item = editorItems[task.task_key]
    const detail = item?.detail
    const source = detail?.sources[0]
    const values = editorValues[task.task_key] || source?.values || {}
    const fields = detail && source
      ? mobileTaskEditorFields(detail, source.editable_fields, values, source.values)
      : []
    const changes = source ? buildMobileTaskChanges(source.values, values, fields) : {}
    const dirtyCount = Object.keys(changes).length
    const editorLoading = loadingEditorKeys.has(task.task_key)
    const editorDisabled = selectionMode || !active || !detail?.writeback_enabled
    const resultNeedsSecondaryFollowup = String(values['核查结果'] || task.summary.result || '').includes('无法核实')
    const hasSecondaryFeedback = Boolean(String(values['二次反馈'] || '').trim())
    const registrationResult = String(values[detail?.workflow.result_field || ''] || '').trim()
    const registrationPropertyState = registrationProperties[task.task_key]
    const linkedRegistrationProperty = detail?.registration_link?.property || null
    const availableRegistrationProperties = [
      ...(linkedRegistrationProperty ? [linkedRegistrationProperty] : []),
      ...(registrationPropertyState?.options || []),
    ].filter((property, index, properties) => (
      properties.findIndex(item => item.id === property.id) === index
    ))

    if (analysisMode && task.review_flow) {
      return (
        <div className="grid gap-3">
          <div className={`mobile-task-table-inline-editor ${toneClass} mobile-task-table-inline-editor--readonly`}>
          <div className="grid min-w-0 gap-2 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <Tag color={task.review_flow.state === 'source_exception' ? 'red' : 'blue'}>
                {task.review_flow.state_label}
              </Tag>
              {task.review_flow.review_due_date && <span>复核截止：{task.review_flow.review_due_date}</span>}
              {['initial_extension', 'deep_extension'].includes(task.review_flow.state) && (
                <span>本轮反馈：{task.review_flow.feedback_submitted ? '已记录' : '未记录'}</span>
              )}
            </div>
            <span className="text-xs text-[var(--app-text-secondary)]">
              两级研判必须在详情中选择成功或失败并填写意见，表格内不提供自由文字保存。
            </span>
          </div>
          <div className="mobile-task-table-inline-actions">
            <Button size="small" type="primary" onClick={() => onOpen(task)}>进入研判详情</Button>
          </div>
          </div>
        </div>
      )
    }

    if (!item) {
      return (
        <div className="grid gap-3">
          <div
            ref={element => setEditorElement(task.task_key, element)}
            className={`mobile-task-table-inline-editor ${toneClass} mobile-task-table-inline-editor--loading`}
            onClick={event => event.stopPropagation()}
          >
            <div className="mobile-task-table-inline-status">
              {editorLoading ? '正在准备本行填写项…' : '滚动到本行时自动读取可编辑信息'}
            </div>
            {!editorLoading && (
              <Button size="small" onClick={() => void requestEditors([task.task_key], true)}>读取本行</Button>
            )}
          </div>
        </div>
      )
    }

    if (!item?.available || !detail || !source) {
      const isModelThree = task.parser_type === '疑似未注销模型三'
      return (
        <div className="grid gap-3">
          <div
            ref={element => setEditorElement(task.task_key, element)}
            className={`mobile-task-table-inline-editor ${toneClass} mobile-task-table-inline-editor--readonly`}
          >
          <div className="mobile-task-table-inline-fields">
            <div className="mobile-task-table-inline-readonly"><span>核查结果</span><strong>{task.summary.result || '未填写'}</strong></div>
            {!isModelThree && <div className="mobile-task-table-inline-readonly">
              <span>{mobileTaskCurrentAddressLabel(task.parser_type, task.summary.result || '')}</span>
              <strong>{task.summary.current_address || '未填写'}</strong>
            </div>}
            {!isModelThree && <div className="mobile-task-table-inline-readonly">
              <span>研判</span>
              <strong>
                <Tooltip title={task.summary.analysis || '未填写'}>
                  <span className="block truncate">{task.summary.analysis || '未填写'}</span>
                </Tooltip>
              </strong>
            </div>}
            {!isModelThree && <div className="mobile-task-table-inline-readonly"><span>二次反馈</span><strong>{task.summary.secondary_feedback || '未填写'}</strong></div>}
            {isModelThree && <div className="mobile-task-table-inline-readonly"><span>备注</span><strong>{task.summary.note || '未填写'}</strong></div>}
          </div>
          <div className="mobile-task-table-inline-actions">
            <Tooltip title={item?.reason || '当前任务只能在详情中处理'}>
              <Button size="small" onClick={() => onOpen(task)}>进入详情</Button>
            </Tooltip>
          </div>
          </div>
        </div>
      )
    }

    return (
      <div className="grid gap-3">
        <div
          ref={element => setEditorElement(task.task_key, element)}
          className={`mobile-task-table-inline-editor ${toneClass}${dirtyCount ? ' mobile-task-table-inline-editor--dirty' : ''}`}
          onClick={event => event.stopPropagation()}
          onDoubleClick={event => event.stopPropagation()}
        >
        {fields.length === 0 ? (
          <div className="mobile-task-table-inline-status">
            {detail.writeback_enabled ? '当前任务没有可填写字段' : '当前任务暂不可编辑，只能查看'}
          </div>
        ) : (
          <fieldset disabled={editorDisabled} className="mobile-task-table-inline-fields mobile-task-table-editable-fields">
            {fields.map(field => {
              const metadata = source.cell_meta[field] || { type: 'text' }
              const resultField = field === detail.workflow.result_field
              const registrationResultField = mobileTaskUsesRegistrationClosure(task.parser_type)
                && resultField
              const registrationAddressField = mobileTaskUsesRegistrationClosure(task.parser_type)
                && field === '现住址'
                && registrationResult === '待登记'
              const options = (resultField
                ? mobileTaskResultOptions(metadata.options, registrationResultField)
                : metadata.options || [])
                .map(option => ({
                  value: String(option.text),
                  label: String(option.text),
                }))
              const isSecondaryFeedback = field === '二次反馈'
              const needsResultUpdate = field === '核查结果'
                && resultNeedsSecondaryFollowup
                && hasSecondaryFeedback
              return (
                <div
                  data-editor-field={field}
                  key={field}
                  className={`mobile-task-table-inline-field${/地址|备注|研判/.test(field) ? ' mobile-task-table-inline-field--wide' : ''}${needsResultUpdate ? ' mobile-task-table-inline-field--attention' : ''}`}
                >
                  <span id={`editor-label-${fieldKey(task.task_key, field)}`}>
                    {field === '核查人'
                      ? '任务分配'
                      : mobileTaskUsesRegistrationClosure(task.parser_type) && field === '现住址'
                        ? mobileTaskCurrentAddressLabel(task.parser_type, registrationResult)
                        : field}
                  </span>
                  {(
                    <small className="mobile-task-field-save-state" aria-live="polite">
                      {saveStates[task.task_key]?.[field] === 'saving' && '保存中'}
                      {saveStates[task.task_key]?.[field] === 'saved' && '已保存'}
                      {saveStates[task.task_key]?.[field] === 'error' && <>
                        <span>未保存，草稿已保留</span>
                        <Button aria-label="重试保存" type="link" size="small" className="h-auto p-0 text-xs" onClick={() => retryAutosave(task.task_key, field)}>重试保存</Button>
                      </>}
                      {saveStates[task.task_key]?.[field] === 'conflict' && <>
                        <span>服务器内容已变化</span>
                        <Button aria-label="核对后重试" type="link" size="small" className="h-auto p-0 text-xs" onClick={() => retryAutosave(task.task_key, field)}>核对后重试</Button>
                      </>}
                    </small>
                  )}
                  {registrationAddressField ? (
                    <div className="grid gap-1">
                      <AutoComplete
                        className="w-full" size="small"
                        value={values[field] || ''}
                        placeholder="输入地址，可直接保存或主动选择房屋"
                        disabled={editorDisabled}
                        options={availableRegistrationProperties.map((property, index) => ({
                          value: `candidate-${index}`, label: registrationPropertyLabel(property), property,
                        }))}
                        onChange={(value, option) => {
                          if (option && !Array.isArray(option) && option.property) return
                          delete chosenPropertyRef.current[task.task_key]
                          setDraft(task.task_key, field, value)
                          scheduleFieldSave(task, item, field, value)
                          window.clearTimeout(registrationSearchSequenceRef.current[`${task.task_key}:timer`])
                          registrationSearchSequenceRef.current[task.task_key] = (registrationSearchSequenceRef.current[task.task_key] || 0) + 1
                          registrationSearchSequenceRef.current[`${task.task_key}:timer`] = window.setTimeout(() => {
                            if (!composingRef.current[task.task_key]?.[field]) void searchRegistrationProperty(task, editorValuesRef.current[task.task_key]?.[field] || '')
                          }, 700)
                        }}
                        onSelect={(_, option) => { if (option.property) void saveRegistrationProperty(task, item, option.property) }}
                      >
                        <Input aria-labelledby={`editor-label-${fieldKey(task.task_key, field)}`} onCompositionStart={() => compositionStart(task.task_key, field)}
                          onCompositionEnd={event => compositionEnd(task, item, field, event.currentTarget.value)}
                          onBlur={() => { cancelScheduledFieldSave(task.task_key, field); void saveField(task, item, field, editorValuesRef.current[task.task_key]?.[field] ?? '') }} />
                      </AutoComplete>
                      <span className="mobile-task-table-inline-hint" aria-live="polite">
                        {registrationPropertyState?.matchStatus === 'matching' ? '正在识别地址…' : registrationPropertyState?.matchStatus === 'unique' ? '根据核查补充信息找到唯一候选，请确认' : registrationPropertyState?.matchStatus === 'multiple' ? '找到多个候选，请选择' : registrationPropertyState?.matchStatus === 'none' ? '未找到正式房屋，可填写待建档地址' : registrationPropertyState?.matchStatus === 'error' ? '地址匹配暂时失败，请重试或填写待建档地址' : '地址会与待登记结果一起保存；房屋候选可按需选择。'}
                      </span>
                    </div>
                  ) : metadata.type === 'select' || field === '核查人' ? (
                    <div className="grid gap-1">
                      <Select
                        aria-labelledby={`editor-label-${fieldKey(task.task_key, field)}`}
                        allowClear
                        showSearch
                        size="small"
                        placeholder="请选择"
                        disabled={editorDisabled}
                        value={values[field] || undefined}
                        options={options}
                        onChange={value => {
                          const nextValue = String(value || '')
                          setDraft(task.task_key, field, nextValue)
                          if (registrationResultField) {
                            delete chosenPropertyRef.current[task.task_key]
                          }
                          void saveField(task, item, field, nextValue)
                        }}
                      />
                      {registrationResultField && registrationResult === '待登记' && (
                        <span className="mobile-task-table-inline-hint">请填写地址；也可主动选择已有房屋。</span>
                      )}
                    </div>
                  ) : (
                    <Input.TextArea
                      aria-labelledby={`editor-label-${fieldKey(task.task_key, field)}`}
                      size="small"
                      placeholder={field === '入住方式' ? '自购、房东出租、中介出租等' : '请输入'}
                      disabled={editorDisabled}
                      autoSize={{ minRows: 1, maxRows: 3 }}
                      value={values[field] || ''}
                      onCompositionStart={() => compositionStart(task.task_key, field)}
                      onCompositionEnd={event => compositionEnd(task, item, field, event.currentTarget.value)}
                      onChange={event => {
                        setDraft(task.task_key, field, event.target.value)
                        scheduleFieldSave(task, item, field, event.target.value)
                      }}
                      onBlur={() => {
                        cancelScheduledFieldSave(task.task_key, field)
                        void saveField(task, item, field, editorValuesRef.current[task.task_key]?.[field] ?? '')
                      }}
                    />
                  )}
                  {isSecondaryFeedback && resultNeedsSecondaryFollowup && (
                    <div
                      className={`mobile-task-table-inline-guidance${hasSecondaryFeedback ? ' mobile-task-table-inline-guidance--active' : ''}`}
                      aria-live={hasSecondaryFeedback ? 'polite' : undefined}
                    >
                      <ExclamationCircleOutlined />
                      <span>
                        {hasSecondaryFeedback
                          ? '已填写二次反馈，请现在将“核查结果”修改为最终结果。'
                          : '二次核查完成后，请务必将“核查结果”修改为最终结果。'}
                      </span>
                    </div>
                  )}
                </div>
              )
            })}
            {!analysisMode && !fields.includes('研判') && task.summary.analysis && (
              <div className="mobile-task-table-inline-readonly">
                <span>研判</span>
                <strong>
                  <Tooltip title={task.summary.analysis}>
                    <span className="block truncate">{task.summary.analysis}</span>
                  </Tooltip>
                </strong>
              </div>
            )}
          </fieldset>
        )}
        {editorErrorKeys.has(task.task_key) && <div className="mobile-task-table-inline-status">
          填写信息核对失败，草稿已保留。
          <Button size="small" onClick={() => void requestEditors([task.task_key], true)}>重新核对</Button>
        </div>}
        </div>
      </div>
    )
  }

  const compactPersonnelColumns: ResponsiveColumns<MobileTaskItem> = responsiveLayout.isCompact ? [{
    title: '人员信息',
    key: 'compact_personnel',
    fixed: 'left',
    width: compactPersonnelPresentation.columnWidth,
    responsivePriority: 'always',
    render: (_, task) => {
      const phones = mobileTaskPhoneOptions(task.summary.phone)
      return (
        <dl className={`mobile-task-table-personnel mobile-task-table-personnel--${compactPersonnelPresentation.layout}`}>
          <div className="mobile-task-table-personnel__row">
            <dt>姓名</dt>
            <dd>
              <button
                type="button"
                className="mobile-task-table-personnel__name"
                onClick={() => onOpen(task)}
              >
                {task.summary.title || '未填写姓名'}
              </button>
            </dd>
          </div>
          <div className="mobile-task-table-personnel__row">
            <dt>身份证号</dt>
            <dd>
              {task.summary.identity_number ? (
                <Button
                  type="link"
                  size="small"
                  className="mobile-task-table-personnel__copy"
                  icon={<CopyOutlined />}
                  onClick={() => onCopy(task.summary.identity_number, '身份证号')}
                >
                  {task.summary.identity_number}
                </Button>
              ) : <span className="text-[var(--app-text-muted)]">未填写</span>}
            </dd>
          </div>
          <div className="mobile-task-table-personnel__row">
            <dt>手机号</dt>
            <dd className="mobile-task-table-personnel__phones">
              {phones.length ? phones.map(phone => (
                <Button
                  key={phone}
                  type="link"
                  size="small"
                  className="mobile-task-table-personnel__copy"
                  icon={<CopyOutlined />}
                  onClick={() => onCopy(phone, '手机号')}
                >
                  {phone}
                </Button>
              )) : <span className="text-[var(--app-text-muted)]">未填写</span>}
            </dd>
          </div>
          <div className="mobile-task-table-personnel__row mobile-task-table-personnel__row--address">
            <dt>原地址</dt>
            <dd>{task.summary.original_address || '未填写'}</dd>
          </div>
        </dl>
      )
    },
  }] : []

  const columns: ResponsiveColumns<MobileTaskItem> = [
    ...compactPersonnelColumns,
    {
      title: '截止日期',
      key: 'deadline',
      fixed: 'left',
      width: 100,
      responsivePriority: 'always',
      render: (_, task) => formatMobileTaskDeadline(task.summary.deadline) || '-',
    },
    {
      title: '社区',
      dataIndex: 'community',
      filters: filterOptions?.community,
      filteredValue: tableFilters?.community || null,
      filterSearch: true,
      width: 105,
      responsivePriority: 'always',
      ellipsis: true,
      render: value => value || <span className="text-[var(--app-text-muted)]">未识别社区</span>,
    },
    {
      title: '小区',
      key: 'small_community',
      filters: filterOptions?.smallCommunity,
      filteredValue: tableFilters?.small_community || null,
      filterSearch: true,
      width: 165,
      responsivePriority: 'standard',
      render: (_, task) => {
        const match = task.address_match
        return (
          <div className="grid min-w-0 gap-1">
            <Tooltip title={match?.small_community_name || '未关联小区'}>
              <span className="truncate">{match?.small_community_name || '未关联小区'}</span>
            </Tooltip>
            <AddressStatusTag task={task} beforeOpen={flushAddressNavigation} onOpen={() => onAddressOpen(task)} />
          </div>
        )
      },
    },
    {
      title: '核查人',
      dataIndex: 'inspector',
      filters: filterOptions?.inspector,
      filteredValue: tableFilters?.inspector || null,
      filterSearch: true,
      width: 105,
      responsivePriority: 'always',
      ellipsis: true,
      render: value => value || <span className="text-[var(--app-text-muted)]">待分配</span>,
    },
    ...(rows.some(task => task.parser_type === '全链条') ? [{
      title: '来源',
      key: 'source',
      width: 130,
      responsivePriority: 'standard' as const,
      render: (_: unknown, task: MobileTaskItem) => {
        const sources = mobileTaskSourceTags(task.summary.source)
        return sources.length
          ? (
              <Tooltip title={sources.join('、')}>
                <div className="mobile-task-source-cloud mobile-task-source-cloud--table">
                  <div>
                    {sources.map(tag => (
                      <Tag key={`${task.task_key}-${tag}`} className="mobile-task-source-cloud__tag">{tag}</Tag>
                    ))}
                  </div>
                </div>
              </Tooltip>
            )
          : <span className="text-[var(--app-text-muted)]">未填写</span>
      },
    }] : []),
    ...(!responsiveLayout.isCompact ? [
      {
        title: '姓名',
        key: 'name',
        width: 110,
        responsivePriority: 'always' as const,
        render: (_: unknown, task: MobileTaskItem) => (
          <button
            type="button"
            className="block max-w-full truncate text-left font-medium text-[var(--app-text-strong)] hover:text-[var(--app-primary)]"
            title={task.summary.title}
            onClick={() => onOpen(task)}
          >
            {task.summary.title || '未填写姓名'}
          </button>
        ),
      },
      {
        title: '身份证号码',
        key: 'identity_number',
        width: 190,
        responsivePriority: 'always' as const,
        sorter: false,
        sortDirections: ['ascend'] as const,
        sortOrder: sort === 'identity_asc' ? 'ascend' as const : null,
        render: (_: unknown, task: MobileTaskItem) => task.summary.identity_number ? (
          <Button
            type="link"
            size="small"
            className="h-auto max-w-full p-0 text-xs"
            icon={<CopyOutlined />}
            onClick={() => onCopy(task.summary.identity_number, '身份证号')}
          >
            <span className="truncate">{task.summary.identity_number}</span>
          </Button>
        ) : <span className="text-[var(--app-text-muted)]">未填写</span>,
      },
      {
        title: '电话',
        key: 'phone',
        width: 150,
        responsivePriority: 'always' as const,
        render: (_: unknown, task: MobileTaskItem) => {
          const phones = mobileTaskPhoneOptions(task.summary.phone)
          const visiblePhones = phones.slice(0, 3)
          if (!visiblePhones.length) return <span className="text-[var(--app-text-muted)]">未填写</span>
          return (
            <div className="flex flex-col items-start">
              {visiblePhones.map(phone => (
                <Button
                  key={phone}
                  type="link"
                  size="small"
                  className="h-auto max-w-full p-0"
                  icon={<CopyOutlined />}
                  onClick={() => onCopy(phone, '手机号')}
                >
                  <span className="truncate">{phone}</span>
                </Button>
              ))}
              {phones.length > visiblePhones.length && (
                <span className="pl-5 text-xs text-[var(--app-text-secondary)]">
                  +{phones.length - visiblePhones.length}
                </span>
              )}
            </div>
          )
        },
      },
      {
        title: '地址',
        key: 'address',
        width: 250,
        responsivePriority: 'always' as const,
        ellipsis: true,
        sorter: false,
        sortDirections: ['ascend'] as const,
        sortOrder: sort === 'address_asc' ? 'ascend' as const : null,
        render: (_: unknown, task: MobileTaskItem) => {
          const address = task.summary.original_address || '未填写'
          return <Tooltip title={address}><span>{address}</span></Tooltip>
        },
      },
    ] : []),
    {
      title: '登记情况',
      dataIndex: ['summary', 'registration_status'],
      width: 110,
      responsivePriority: 'wide',
      ellipsis: true,
      render: value => value || <span className="text-[var(--app-text-muted)]">未填写</span>,
    },
    {
      title: '状态',
      key: 'state',
      width: 250,
      responsivePriority: 'always',
      render: (_, task) => {
        const state = STATE_LABELS[task.state]
        return (
          <div className="grid gap-2">
            <div className="flex flex-wrap gap-1">
              <Tag color={state.color}>{state.text}</Tag>
              {task.needs_review && <Tag color="warning" icon={<ExclamationCircleOutlined />}>需复核</Tag>}
              {task.review_stage === 'analyzed' && <Tag color="purple">已研判</Tag>}
              {task.review_stage === 'initial_pending' && <Tag color="volcano">初步待研判</Tag>}
              {task.review_stage === 'initial_extension' && <Tag color="gold">初步复核中</Tag>}
              {task.review_stage === 'deep_pending' && <Tag color="purple">深度待研判</Tag>}
              {task.review_stage === 'deep_extension' && <Tag color="geekblue">深度复核中</Tag>}
              {task.review_stage === 'final_unverifiable' && <Tag color="red">最终无法核实</Tag>}
              {task.review_stage === 'source_exception' && <Tag color="red">流程已暂停</Tag>}
              {(task.conflict || task.source_count > 1) && <Tag color="red">来源异常</Tag>}
              {task.sync_state === 'conflict' && <Tag color="red">同步冲突</Tag>}
              {task.sync_state === 'retry' && <Tag color="orange">同步重试</Tag>}
              {task.sync_state === 'pending' && <Tag color="blue">待同步</Tag>}
              {task.watch_marks?.map(mark => (
                <Tag key={`${task.task_key}-${mark.category_id}`} color={mark.color}>{mark.name}</Tag>
              ))}
              {task.qmf_status && <QmfFeedbackStatus status={task.qmf_status} compact />}
              <ResidenceRegistrationStatus status={task.residence_status} compact />
              <RegistrationLinkStatus link={task.registration_link} compact />
            </div>
            {task.review_flow && !['resolved', 'archived'].includes(task.review_flow.state) && (
              <UnverifiableReviewNotice flow={task.review_flow} />
            )}
          </div>
        )
      },
    },
  ]

  const visibleColumns = useMemo(
    () => getResponsiveColumns(columns, responsiveLayout.mode) as TableColumnsType<MobileTaskItem>,
    [columns, responsiveLayout.mode],
  )
  const tableScrollWidth = responsiveLayout.isCompact
    ? compactPersonnelPresentation.tableScrollWidth
    : responsiveLayout.isStandard
      ? 1555
      : 1665

  return (
    <div ref={tableRef} className="app-card mobile-task-table overflow-hidden">
      <Table<MobileTaskItem>
        rowKey="task_key"
        size="middle"
        loading={loading}
        dataSource={rows}
        columns={visibleColumns}
        tableLayout="fixed"
        scroll={{ x: tableScrollWidth }}
        rowSelection={selectionMode ? {
          selectedRowKeys,
          hideSelectAll: true,
          columnWidth: 48,
          getCheckboxProps: task => ({ disabled: !canSelect(task) }),
          onSelect,
        } : undefined}
        expandable={{
          expandedRowKeys: rows.map(task => task.task_key),
          showExpandColumn: false,
          expandedRowRender: renderExpandedRow,
        }}
        pagination={false}
        onChange={(_, filters, sorter, extra) => {
          if (extra.action === 'filter') {
            onTableFiltersChange?.(filters as Record<string, Key[] | null>)
            return
          }
          const activeSorter = Array.isArray(sorter) ? sorter[0] : sorter
          if (activeSorter.order !== 'ascend') {
            onSortChange('priority')
          } else if (activeSorter.columnKey === 'identity_number') {
            onSortChange('identity_asc')
          } else if (activeSorter.columnKey === 'address') {
            onSortChange('address_asc')
          }
        }}
        onRow={task => ({
          'data-mobile-task-row-key': task.task_key,
          className: [
            'mobile-task-table-primary-row',
            `mobile-task-table-primary-row--tone-${mobileTaskSurfaceTone(task)}`,
            selectionMode && selectedRowKeys.includes(task.task_key) ? 'mobile-task-table-row-selected' : '',
          ].filter(Boolean).join(' '),
          onDoubleClick: () => onOpen(task),
        })}
      />
    </div>
  )
}

