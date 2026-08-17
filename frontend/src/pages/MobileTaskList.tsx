import {
  CheckSquareOutlined,
  CopyOutlined,
  ExclamationCircleOutlined,
  PhoneOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import { Alert, Button, Empty, Input, Modal, Progress, Segmented, Select, Skeleton, Tag, message } from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState, type SyntheticEvent } from 'react'
import { useNavigate, useNavigationType, useSearchParams } from 'react-router-dom'
import {
  getMobileTaskFilterOptions,
  bulkAssignMobileTasks,
  listMobileTasks,
  MOBILE_TASK_ASSIGNMENT_CHUNK_SIZE,
  selectMobileTasksForAssignment,
  type MobileTaskFacets,
  type MobileTaskFilterOption,
  type MobileTaskItem,
  type MobileTaskPriority,
  type MobileTaskReviewStage,
  type MobileTaskScope,
  type MobileTaskSort,
  type MobileTaskStatus,
} from '../api/client'
import { useAuth } from '../context/AuthContext'
import { isFlowTaskElevated, MOBILE_TASK_TYPES } from '../utils/mobileTaskRouting'
import {
  formatMobileTaskDeadline,
  mobileTaskCanLaunchTelephone,
  mobileTaskPhoneOptions,
  mobileTaskSourceTags,
  mobileTaskSurfaceTone,
} from '../utils/mobileTasks'
import {
  clearMobileTaskListRestoration,
  clearMobileTaskListSnapshot,
  readMobileTaskListRestoration,
  readMobileTaskListSnapshot,
  writeMobileTaskListRestoration,
  writeMobileTaskListSnapshot,
  type MobileTaskListRestoration,
} from '../utils/mobileTaskListState'
import MobilePhonePicker from '../components/MobilePhonePicker'
import MobileTaskTable from '../components/MobileTaskTable'
import { ListToolbar } from '../components/ui'
import useDebouncedValue from '../hooks/useDebouncedValue'

const STATUS_OPTIONS = [
  { label: '待处理（未完成）', value: 'pending' },
  { label: '未核查', value: 'unchecked' },
  { label: '已核查未完成', value: 'checked' },
  { label: '已完成', value: 'completed' },
  { label: '全部', value: 'all' },
] satisfies Array<{ label: string; value: MobileTaskStatus }>

const PRIORITY_OPTIONS = [
  { label: '全部优先级', value: 'all' },
  { label: '已研判', value: 'analyzed' },
  { label: '来源异常', value: 'source_exception' },
  { label: '普通待处理', value: 'ordinary' },
  { label: '等待研判', value: 'waiting_analysis' },
  { label: '已完成', value: 'completed' },
] satisfies Array<{ label: string; value: MobileTaskPriority }>

const SORT_OPTIONS = [
  { label: '默认优先级', value: 'priority' },
  { label: '最近更新', value: 'updated_desc' },
  { label: '最早更新', value: 'updated_asc' },
] satisfies Array<{ label: string; value: MobileTaskSort }>

const STATE_LABELS = {
  unchecked: { text: '未核查', color: 'gold' },
  checked: { text: '待补结果', color: 'orange' },
  completed: { text: '已完成', color: 'green' },
} as const

const PRIORITY_CARDS: Array<{ key: MobileTaskPriority; label: string }> = [
  { key: 'analyzed', label: '已研判' },
  { key: 'source_exception', label: '来源异常' },
  { key: 'ordinary', label: '普通待处理' },
  { key: 'waiting_analysis', label: '等待研判' },
  { key: 'completed', label: '已完成' },
  { key: 'all', label: '全部' },
]

const EMPTY_FACETS: MobileTaskFacets = {
  total: 0,
  priority_counts: {
    analyzed: 0,
    source_exception: 0,
    pending_sync: 0,
    ordinary: 0,
    waiting_analysis: 0,
    completed: 0,
  },
  status_counts: { unchecked: 0, checked: 0, completed: 0 },
}

const EMPTY_ASSIGNMENT = {
  enabled: false,
  community_aliases: {} as Record<string, string>,
  inspectors_by_community: {} as Record<string, string[]>,
}

interface BulkAssignmentProgress {
  total: number
  processed: number
  updated: number
  skipped: number
  failed: number
  assignmentCounts: Record<string, number>
  details: Array<{ row_key: string; reason: string }>
  failedDetails: Array<{ row_key: string; reason: string }>
  error: string
}

function bulkSkipSummary(details: Array<{ row_key: string; reason: string }>) {
  const counts = details.reduce<Record<string, number>>((result, item) => {
    result[item.reason] = (result[item.reason] || 0) + 1
    return result
  }, {})
  return Object.entries(counts)
    .map(([reason, count]) => `${reason} ${count}条`)
    .join('、')
}

function readMulti(searchParams: URLSearchParams, key: string) {
  return searchParams.getAll(key).filter(Boolean)
}

function readMultiNumber(searchParams: URLSearchParams, key: string) {
  return searchParams.getAll(key).map(Number).filter(value => Number.isInteger(value) && value > 0)
}

function readPriority(value: string | null): MobileTaskPriority {
  return PRIORITY_OPTIONS.some(option => option.value === value)
    ? value as MobileTaskPriority
    : 'all'
}

function readSort(value: string | null): MobileTaskSort {
  return SORT_OPTIONS.some(option => option.value === value)
    ? value as MobileTaskSort
    : 'priority'
}

export default function MobileTaskList({ mode = 'tasks' }: { mode?: 'tasks' | 'analysis' }) {
  const navigate = useNavigate()
  const navigationType = useNavigationType()
  const { recordActivity, user } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const requestedType = searchParams.get('type') || MOBILE_TASK_TYPES[0]
  const parserType = MOBILE_TASK_TYPES.includes(requestedType as any)
    ? requestedType
    : MOBILE_TASK_TYPES[0]
  const requestedScope = searchParams.get('scope')
  const adminMode = isFlowTaskElevated(
    user?.member?.position,
    user?.role,
    user?.permission_groups?.map(group => group.code),
    user?.permissions,
  )
  const analysisOnly = mode === 'analysis'
  const scope: MobileTaskScope = analysisOnly || adminMode
    ? 'all'
    : requestedScope === 'community' ? 'community' : 'mine'
  const requestedStatus = searchParams.get('status')
  const requestedReviewStage = searchParams.get('review_stage')
  const [status, setStatus] = useState<MobileTaskStatus>(
    analysisOnly
      ? 'all'
      : ['pending', 'unchecked', 'checked', 'review', 'completed', 'all'].includes(requestedStatus || '')
      ? requestedStatus as MobileTaskStatus
      : 'pending',
  )
  const [reviewStage, setReviewStage] = useState<MobileTaskReviewStage>(
    analysisOnly
      ? 'waiting_analysis'
      : ['waiting_analysis', 'analyzed'].includes(requestedReviewStage || '')
      ? requestedReviewStage as MobileTaskReviewStage
      : 'all',
  )
  const [communities, setCommunities] = useState<string[]>(readMulti(searchParams, 'community'))
  const [inspectors, setInspectors] = useState<string[]>(readMulti(searchParams, 'inspector'))
  const [watchCategories, setWatchCategories] = useState<number[]>(readMultiNumber(searchParams, 'watch_category'))
  const [priority, setPriority] = useState<MobileTaskPriority>(readPriority(searchParams.get('priority')))
  const [sort, setSort] = useState<MobileTaskSort>(readSort(searchParams.get('sort')))
  const taskDisplayMode = user?.task_display_mode || 'card'
  const restorationRef = useRef<MobileTaskListRestoration | null | undefined>(undefined)
  const snapshotRef = useRef<ReturnType<typeof readMobileTaskListSnapshot> | undefined>(undefined)
  if (restorationRef.current === undefined) {
    restorationRef.current = navigationType === 'POP'
      ? readMobileTaskListRestoration(window.sessionStorage, mode, taskDisplayMode)
      : null
    snapshotRef.current = navigationType === 'POP'
      ? readMobileTaskListSnapshot(mode, taskDisplayMode)
      : null
    if (navigationType !== 'POP') {
      clearMobileTaskListRestoration(window.sessionStorage)
      clearMobileTaskListSnapshot()
    }
  }
  const restorationStartedRef = useRef(false)
  const pageRootRef = useRef<HTMLDivElement>(null)
  const [keywordInput, setKeywordInput] = useState(() => restorationRef.current?.keyword || '')
  const [keywordFlush, setKeywordFlush] = useState(0)
  const keyword = useDebouncedValue(keywordInput.trim(), 350, keywordFlush)
  const [communityOptions, setCommunityOptions] = useState<MobileTaskFilterOption[]>([])
  const [inspectorOptions, setInspectorOptions] = useState<MobileTaskFilterOption[]>([])
  const [assignment, setAssignment] = useState(EMPTY_ASSIGNMENT)
  const [watchCategoryOptions, setWatchCategoryOptions] = useState<Array<{ value: number; label: string; color: string; alert_level: string; count: number }>>([])
  const [facets, setFacets] = useState<MobileTaskFacets>(() => snapshotRef.current?.facets || EMPTY_FACETS)
  const [rows, setRows] = useState<MobileTaskItem[]>(() => snapshotRef.current?.rows || [])
  const [total, setTotal] = useState(() => snapshotRef.current?.total || 0)
  const [page, setPage] = useState(() => snapshotRef.current?.page || 1)
  const [loading, setLoading] = useState(() => !snapshotRef.current)
  const [loadingMore, setLoadingMore] = useState(false)
  const [optionsLoading, setOptionsLoading] = useState(false)
  const [moreOpen, setMoreOpen] = useState(false)
  const [error, setError] = useState('')
  const [sourceMessage, setSourceMessage] = useState(() => snapshotRef.current?.source_message || '')
  const [selectionMode, setSelectionMode] = useState(false)
  const [selectedRows, setSelectedRows] = useState<Set<string>>(new Set())
  const [bulkOpen, setBulkOpen] = useState(false)
  const [bulkMode, setBulkMode] = useState<'single' | 'balanced'>('single')
  const [bulkInspector, setBulkInspector] = useState<string | undefined>()
  const [bulkSaving, setBulkSaving] = useState(false)
  const [bulkProgress, setBulkProgress] = useState<BulkAssignmentProgress | null>(null)
  const [selectingAll, setSelectingAll] = useState(false)
  const [selectionCommunity, setSelectionCommunity] = useState('')
  const loadMoreRef = useRef<HTMLDivElement>(null)
  const loadingMoreRef = useRef(false)
  const optionsRequestId = useRef(0)
  const listRequestId = useRef(0)
  const loadedPageRef = useRef(snapshotRef.current?.loaded_page || 1)
  const canBulkAssign = assignment.enabled
  const assignmentCommunity = useCallback((task: MobileTaskItem) => (
    assignment.community_aliases[String(task.community || '').trim()] || ''
  ), [assignment.community_aliases])
  const selectedTasks = useMemo(
    () => rows.filter(task => selectedRows.has(task.row_key)),
    [rows, selectedRows],
  )
  const selectedCommunities = useMemo(() => Array.from(new Set(
    selectedTasks.map(assignmentCommunity).filter(Boolean),
  )), [assignmentCommunity, selectedTasks])
  const selectedCommunity = selectionCommunity || (
    selectedCommunities.length === 1 ? selectedCommunities[0] : ''
  )
  const bulkInspectorOptions = selectedCommunity
    ? assignment.inspectors_by_community[selectedCommunity] || []
    : []
  const selectedCount = selectedRows.size
  const isTaskAssignable = useCallback((task: MobileTaskItem) => {
    const community = assignmentCommunity(task)
    return canBulkAssign
      && !task.inspector
      && task.state !== 'completed'
      && !task.conflict
      && Boolean(community)
      && Boolean(assignment.inspectors_by_community[community]?.length)
  }, [assignment.inspectors_by_community, assignmentCommunity, canBulkAssign])
  const canSelectTask = useCallback((task: MobileTaskItem) => {
    const community = assignmentCommunity(task)
    return isTaskAssignable(task)
      && (!selectedCommunity || selectedCommunity === community)
  }, [assignmentCommunity, isTaskAssignable, selectedCommunity])

  const openTask = useCallback((task: MobileTaskItem) => {
    const scrollContainer = pageRootRef.current?.closest('main')
    writeMobileTaskListSnapshot({
      mode,
      display_mode: taskDisplayMode,
      rows,
      total,
      page,
      loaded_page: loadedPageRef.current,
      facets,
      source_message: sourceMessage,
      saved_at: Date.now(),
    })
    writeMobileTaskListRestoration(window.sessionStorage, {
      version: 1,
      mode,
      return_url: `${window.location.pathname}${window.location.search}`,
      display_mode: taskDisplayMode,
      scroll_top: scrollContainer?.scrollTop || window.scrollY,
      page,
      loaded_page: loadedPageRef.current,
      keyword: keywordInput,
      row_key: task.row_key,
      saved_at: Date.now(),
    })
    navigate(`${analysisOnly ? '/police-analysis' : '/tasks'}/${encodeURIComponent(task.parser_type)}/${task.row_key}?scope=${scope}`)
  }, [analysisOnly, facets, keywordInput, mode, navigate, page, rows, scope, sourceMessage, taskDisplayMode, total])

  useEffect(() => {
    setSelectionMode(false)
    setSelectedRows(new Set())
    setSelectionCommunity('')
    setBulkInspector(undefined)
    setBulkMode('single')
    setBulkProgress(null)
  }, [parserType, scope, status, reviewStage, priority, sort, keyword, communities, inspectors, watchCategories])

  const toggleSelected = (rowKey: string, checked: boolean) => {
    const task = rows.find(item => item.row_key === rowKey)
    const community = task ? assignmentCommunity(task) : ''
    if (checked && selectedCommunity && community !== selectedCommunity) {
      message.warning(`本次已锁定为${selectedCommunity}，请分开选择其他社区任务`)
      return
    }
    if (checked && !selectedCommunity) setSelectionCommunity(community)
    if (!checked && selectedRows.size === 1 && selectedRows.has(rowKey)) {
      setSelectionCommunity('')
    }
    setSelectedRows(current => {
      const next = new Set(current)
      if (checked) next.add(rowKey)
      else next.delete(rowKey)
      return next
    })
  }

  const selectAllFiltered = async () => {
    setSelectingAll(true)
    try {
      const result = await selectMobileTasksForAssignment({
        parser_type: parserType,
        scope: analysisOnly ? 'all' : scope,
        status: analysisOnly ? 'all' : status,
        review_stage: reviewStage,
        communities,
        inspectors,
        watch_categories: watchCategories,
        priority: analysisOnly ? 'all' : priority,
        sort,
        keyword: keyword || undefined,
        page: 1,
        page_size: 50,
      })
      if (!result.total) {
        setSelectedRows(new Set())
        setSelectionCommunity('')
        message.info('当前筛选中没有可分配的未处理任务')
        return
      }
      setSelectedRows(new Set(result.row_keys))
      setSelectionCommunity(result.community)
      message.success(`已选择当前筛选中的 ${result.total} 条可分配任务`)
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '全选当前筛选失败')
    } finally {
      setSelectingAll(false)
    }
  }

  const clearSelection = () => {
    setSelectedRows(new Set())
    setSelectionCommunity('')
    setBulkProgress(null)
  }

  const leaveSelectionMode = () => {
    setSelectionMode(false)
    clearSelection()
    setBulkInspector(undefined)
    setBulkMode('single')
  }

  const submitBulkAssignment = async () => {
    if (!selectedRows.size || (bulkMode === 'single' && !bulkInspector)) return
    const rowKeys = [...selectedRows]
    const resumed = bulkProgress?.error && bulkProgress.total === rowKeys.length
      ? bulkProgress
      : null
    let processed = resumed?.processed || 0
    let updated = resumed?.updated || 0
    let skipped = resumed?.skipped || 0
    let failed = resumed?.failed || 0
    let details = resumed?.details || []
    let failedDetails = resumed?.failedDetails || []
    const assignmentCounts = { ...(resumed?.assignmentCounts || {}) }
    setBulkSaving(true)
    setBulkProgress({
      total: rowKeys.length,
      processed,
      updated,
      skipped,
      failed,
      assignmentCounts,
      details,
      failedDetails,
      error: '',
    })
    try {
      for (let offset = processed; offset < rowKeys.length; offset += MOBILE_TASK_ASSIGNMENT_CHUNK_SIZE) {
        const chunk = rowKeys.slice(offset, offset + MOBILE_TASK_ASSIGNMENT_CHUNK_SIZE)
        const result = await bulkAssignMobileTasks(parserType, {
          row_keys: chunk,
          inspector: bulkMode === 'single' ? bulkInspector : undefined,
          mode: bulkMode,
          balanced_offset: bulkMode === 'balanced' ? offset : undefined,
          balanced_total: bulkMode === 'balanced' ? rowKeys.length : undefined,
        })
        processed = offset + chunk.length
        updated += result.updated
        skipped += result.skipped
        failed += result.failed
        details = [...details, ...result.details]
        failedDetails = [...failedDetails, ...(result.failed_details || [])]
        Object.entries(result.assignment_counts).forEach(([name, count]) => {
          assignmentCounts[name] = (assignmentCounts[name] || 0) + count
        })
        setBulkProgress({
          total: rowKeys.length,
          processed,
          updated,
          skipped,
          failed,
          assignmentCounts: { ...assignmentCounts },
          details,
          failedDetails,
          error: '',
        })
      }
      if (updated) {
        if (bulkMode === 'balanced') {
          const summary = Object.entries(assignmentCounts)
            .filter(([, count]) => count > 0)
            .map(([name, count]) => `${name} ${count}条`)
            .join('、')
          message.success(`已平均分配 ${updated} 条任务${summary ? `：${summary}` : ''}`)
        } else {
          message.success(`已分配 ${updated} 条任务给 ${bulkInspector}`)
        }
      }
      if (skipped) message.warning(`有 ${skipped} 条任务已分配、已变化或不再符合条件，列表已刷新`)
      if (failed) message.error(`有 ${failed} 条任务写入失败：${bulkSkipSummary(failedDetails)}`)
      setSelectionMode(false)
      clearSelection()
      setBulkOpen(false)
      setBulkInspector(undefined)
      setBulkMode('single')
      await load(1)
    } catch (reason: any) {
      const errorMessage = reason?.response?.data?.detail || reason?.message || '批量分配失败'
      setBulkProgress({
        total: rowKeys.length,
        processed,
        updated,
        skipped,
        failed,
        assignmentCounts: { ...assignmentCounts },
        details,
        failedDetails,
        error: errorMessage,
      })
      message.error(`分配在 ${processed}/${rowKeys.length} 条后中断，可直接继续，不会覆盖已成功任务`)
    } finally {
      setBulkSaving(false)
    }
  }

  const loadOptions = useCallback(async () => {
    const requestId = ++optionsRequestId.current
    setOptionsLoading(true)
    try {
      const result = await getMobileTaskFilterOptions(
        parserType,
        scope,
        communities,
        analysisOnly ? reviewStage : 'all',
      )
      if (requestId !== optionsRequestId.current) return
      setCommunityOptions(result.communities)
      setInspectorOptions(result.inspectors)
      setAssignment(result.assignment || EMPTY_ASSIGNMENT)
      setWatchCategoryOptions(result.watch_categories || [])
      const communityValues = new Set(result.communities.map(option => option.value))
      const inspectorValues = new Set(result.inspectors.map(option => option.value))
      setCommunities(current => {
        const next = current.filter(value => communityValues.has(value))
        return next.length === current.length ? current : next
      })
      setInspectors(current => {
        const next = current.filter(value => inspectorValues.has(value))
        return next.length === current.length ? current : next
      })
      const watchValues = new Set((result.watch_categories || []).map(option => option.value))
      setWatchCategories(current => current.filter(value => watchValues.has(value)))
    } catch {
      if (requestId !== optionsRequestId.current) return
      setCommunityOptions([])
      setInspectorOptions([])
      setAssignment(EMPTY_ASSIGNMENT)
      setWatchCategoryOptions([])
    } finally {
      if (requestId === optionsRequestId.current) setOptionsLoading(false)
    }
  }, [analysisOnly, communities, parserType, reviewStage, scope])

  useEffect(() => { void loadOptions() }, [loadOptions])

  useEffect(() => {
    if (bulkInspector && !bulkInspectorOptions.includes(bulkInspector)) {
      setBulkInspector(undefined)
    }
  }, [bulkInspector, bulkInspectorOptions])

  const load = useCallback(async (
    targetPage = 1,
    append = false,
    silent = false,
    restorePageCount = 0,
  ) => {
    if (append && loadingMoreRef.current) return
    if (append) loadingMoreRef.current = true
    const requestId = ++listRequestId.current
    if (!silent) {
      append ? setLoadingMore(true) : setLoading(true)
      setError('')
    }
    try {
      const requestPage = (requestedPage: number) => listMobileTasks({
          parser_type: parserType,
          scope: analysisOnly ? 'all' : scope,
          status: analysisOnly ? 'all' : status,
          review_stage: reviewStage,
          communities,
          inspectors,
          watch_categories: watchCategories,
          priority: analysisOnly ? 'all' : priority,
          sort,
          keyword: keyword || undefined,
          page: requestedPage,
          page_size: 50,
        })
      const refreshPageCount = Math.max(1, restorePageCount || loadedPageRef.current)
      let results
      if (silent || restorePageCount > 0) {
        results = []
        for (let requestedPage = 1; requestedPage <= refreshPageCount; requestedPage += 1) {
          results.push(await requestPage(requestedPage))
        }
      } else {
        results = [await requestPage(targetPage)]
      }
      if (requestId !== listRequestId.current) return
      const result = results[0]
      const refreshedRows = results.flatMap(item => item.data)
      setRows(current => append ? [...current, ...result.data] : refreshedRows)
      if (restorePageCount > 0 && refreshedRows.length === 0) {
        clearMobileTaskListRestoration(window.sessionStorage)
        restorationRef.current = null
      }
      setTotal(result.total)
      if (!silent || restorePageCount > 0) {
        setPage(targetPage)
        loadedPageRef.current = targetPage
      }
      setFacets(result.facets || EMPTY_FACETS)
      setSourceMessage(result.message || '')
    } catch (reason: any) {
      if (requestId !== listRequestId.current) return
      if (!silent) {
        setError(reason?.response?.data?.detail || reason?.message || '任务列表读取失败')
        if (!append) setRows([])
      }
      if (restorePageCount > 0) {
        clearMobileTaskListRestoration(window.sessionStorage)
        restorationRef.current = null
      }
    } finally {
      if (!silent && requestId === listRequestId.current) {
        setLoading(false)
        setLoadingMore(false)
      }
      if (append) loadingMoreRef.current = false
    }
  }, [analysisOnly, communities, inspectors, keyword, parserType, priority, reviewStage, scope, sort, status, watchCategories])

  useEffect(() => {
    const sentinel = loadMoreRef.current
    if (!sentinel || loading || loadingMore || rows.length >= total) return undefined
    const root = pageRootRef.current?.closest('main') || null
    const observer = new IntersectionObserver(entries => {
      if (!entries.some(entry => entry.isIntersecting)) return
      void load(page + 1, true)
    }, { root, rootMargin: '720px 0px' })
    observer.observe(sentinel)
    return () => observer.disconnect()
  }, [load, loading, loadingMore, page, rows.length, total])

  useEffect(() => {
    const restoration = restorationRef.current
    if (restoration) {
      if (!restorationStartedRef.current) {
        restorationStartedRef.current = true
        void load(
          restoration.page,
          false,
          Boolean(snapshotRef.current),
          restoration.loaded_page,
        )
      }
      return
    }
    void load()
  }, [load])

  useEffect(() => {
    const restoration = restorationRef.current
    if (!restoration || loading || rows.length === 0) return undefined

    let frame = 0
    let attempts = 0
    const restore = () => {
      attempts += 1
      const scrollContainer = pageRootRef.current?.closest('main') as HTMLElement | null
      if (scrollContainer) {
        const maxScrollTop = Math.max(0, scrollContainer.scrollHeight - scrollContainer.clientHeight)
        scrollContainer.scrollTop = Math.min(restoration.scroll_top, maxScrollTop)
        if (Math.abs(scrollContainer.scrollTop - restoration.scroll_top) <= 16 || attempts >= 20) {
          if (Math.abs(scrollContainer.scrollTop - restoration.scroll_top) > 16) {
            const anchor = Array.from(
              pageRootRef.current?.querySelectorAll<HTMLElement>('[data-mobile-task-row-key]') || [],
            ).find(element => element.dataset.mobileTaskRowKey === restoration.row_key)
            anchor?.scrollIntoView({ block: 'center' })
          }
          clearMobileTaskListRestoration(window.sessionStorage)
          restorationRef.current = null
          return
        }
      } else {
        window.scrollTo({ top: restoration.scroll_top, behavior: 'auto' })
        clearMobileTaskListRestoration(window.sessionStorage)
        restorationRef.current = null
        return
      }
      frame = window.requestAnimationFrame(restore)
    }
    frame = window.requestAnimationFrame(restore)

    return () => {
      window.cancelAnimationFrame(frame)
    }
  }, [loading, rows])

  useEffect(() => {
    const refreshVisibleList = () => {
      if (restorationRef.current) return
      if (document.visibilityState === 'visible') void load(1, false, true)
    }
    const visibilityChanged = () => {
      if (document.visibilityState === 'visible') refreshVisibleList()
    }
    const timer = window.setInterval(refreshVisibleList, 30_000)
    window.addEventListener('focus', refreshVisibleList)
    document.addEventListener('visibilitychange', visibilityChanged)
    return () => {
      window.clearInterval(timer)
      window.removeEventListener('focus', refreshVisibleList)
      document.removeEventListener('visibilitychange', visibilityChanged)
    }
  }, [load])

  useEffect(() => {
    const next = new URLSearchParams()
    next.set('type', parserType)
    next.set('scope', analysisOnly ? 'all' : scope)
    next.set('status', analysisOnly ? 'all' : status)
    if (analysisOnly) next.set('review_stage', reviewStage)
    else if (reviewStage !== 'all') next.set('review_stage', reviewStage)
    communities.forEach(value => next.append('community', value))
    inspectors.forEach(value => next.append('inspector', value))
    watchCategories.forEach(value => next.append('watch_category', String(value)))
    if (!analysisOnly && priority !== 'all') next.set('priority', priority)
    if (sort !== 'priority') next.set('sort', sort)
    setSearchParams(next, { replace: true })
  }, [analysisOnly, communities, inspectors, parserType, priority, reviewStage, scope, setSearchParams, sort, status, watchCategories])

  const updateQuery = (type: string, nextScope: MobileTaskScope) => {
    const next = new URLSearchParams()
    next.set('type', type)
    next.set('scope', nextScope)
    next.set('status', 'pending')
    setCommunities([])
    setInspectors([])
    setWatchCategories([])
    setPriority('all')
    setSort('priority')
    setStatus(analysisOnly ? 'all' : 'pending')
    setReviewStage(analysisOnly ? 'waiting_analysis' : 'all')
    setSearchParams(next)
  }

  const clearFilters = () => {
    setCommunities([])
    setInspectors([])
    setWatchCategories([])
    setPriority('all')
    setSort('priority')
    setStatus(analysisOnly ? 'all' : 'pending')
    setReviewStage(analysisOnly ? 'waiting_analysis' : 'all')
    setKeywordInput('')
  }

  const selectPriorityCard = (nextPriority: MobileTaskPriority) => {
    setReviewStage('all')
    setStatus('all')
    if (nextPriority === 'all') {
      setPriority('all')
      return
    }
    setPriority(nextPriority)
  }

  const filtersActive = communities.length > 0
    || inspectors.length > 0
    || watchCategories.length > 0
    || (!analysisOnly && priority !== 'all')
    || (!analysisOnly && status !== 'pending')
    || (!analysisOnly && reviewStage !== 'all')
    || sort !== 'priority'
    || Boolean(keywordInput.trim())

  const dial = async (phone: string) => {
    await recordActivity().catch(() => {})
    const navigation = navigator as Navigator & { userAgentData?: { mobile?: boolean } }
    if (!mobileTaskCanLaunchTelephone(
      navigation.userAgent,
      navigation.userAgentData?.mobile,
      navigation.maxTouchPoints,
    )) {
      await navigator.clipboard.writeText(phone)
      message.info('当前设备没有拨号功能，已复制电话号码')
      return
    }
    window.location.href = `tel:${phone}`
  }

  const copyValue = async (
    value: string,
    label: '身份证号' | '手机号',
  ) => {
    try {
      await navigator.clipboard.writeText(value)
      message.success(`${label}已复制`)
    } catch {
      message.error(`${label}复制失败，请长按或选中文字复制`)
    }
  }

  const copyCardValue = async (
    event: SyntheticEvent,
    value: string,
    label: '身份证号' | '手机号',
  ) => {
    event.stopPropagation()
    await copyValue(value, label)
  }

  return (
    <div ref={pageRootRef} className="mobile-task-page">
      <ListToolbar
        className="mobile-task-filter-card"
        filters={<div className="mobile-task-filter-grid">
          <Select
            size="large"
            value={parserType}
            onChange={value => updateQuery(value, scope)}
            options={MOBILE_TASK_TYPES.map(value => ({ value, label: value }))}
          />
          {analysisOnly ? (
            <Segmented
              className="mobile-task-scope-switch"
              value={reviewStage}
              options={[
                { label: '待研判', value: 'waiting_analysis' },
                { label: '已研判', value: 'analyzed' },
              ]}
              onChange={value => setReviewStage(value as MobileTaskReviewStage)}
            />
          ) : adminMode ? (
            <Tag color="blue" className="mobile-task-scope-tag">全所</Tag>
          ) : (
            <Segmented
              className="mobile-task-scope-switch"
              value={scope}
              onChange={value => updateQuery(parserType, value as MobileTaskScope)}
              options={[{ label: '我的', value: 'mine' }, { label: '社区', value: 'community' }]}
            />
          )}
          <Select
            mode="multiple"
            size="large"
            value={communities}
            loading={optionsLoading}
            maxTagCount="responsive"
            showSearch
            allowClear
            optionFilterProp="label"
            placeholder="筛选社区"
            options={communityOptions.map(option => ({
              value: option.value,
              label: `${option.label}（${option.count}）`,
            }))}
            onChange={values => {
              setCommunities(values)
              setInspectors([])
              setInspectorOptions([])
            }}
          />
          <Select
            mode="multiple"
            size="large"
            value={inspectors}
            loading={optionsLoading}
            maxTagCount="responsive"
            showSearch
            allowClear
            optionFilterProp="label"
            placeholder="筛选核查人"
            options={inspectorOptions.map(option => ({
              value: option.value,
              label: `${option.label}（${option.count}）`,
            }))}
            onChange={values => setInspectors(values)}
          />
          <div className="mobile-task-filter-search flex gap-2">
            <Input
              allowClear
              value={keywordInput}
              prefix={<SearchOutlined />}
              placeholder="搜索姓名、电话或地址"
              onChange={event => setKeywordInput(event.target.value)}
              onPressEnter={() => setKeywordFlush(current => current + 1)}
            />
          </div>
        </div>}
        extra={<>
          {!analysisOnly && <div className="mobile-task-priority-grid" aria-label="任务快捷筛选">
          {PRIORITY_CARDS.map(card => {
            const count = card.key === 'all'
              ? facets.total
              : facets.priority_counts[card.key]
            const active = card.key === 'all'
              ? priority === 'all' && status === 'all'
              : priority === card.key
            return (
              <button
                key={card.key}
                type="button"
                className={`mobile-task-priority-card${active ? ' is-active' : ''}`}
                onClick={() => selectPriorityCard(card.key)}
              >
                <span>{card.label}</span>
                <strong>{count}</strong>
              </button>
            )
          })}
          </div>}

        <div className="mobile-task-more-toggle">
          <Button type="link" onClick={() => setMoreOpen(value => !value)}>
            {moreOpen ? '收起更多筛选' : '更多筛选'}
          </Button>
          {filtersActive && (
            <Button type="link" onClick={clearFilters}>清除筛选</Button>
          )}
        </div>
        {moreOpen && (
          <div className="mobile-task-more-grid">
            {!analysisOnly && <Select
              value={status}
              options={STATUS_OPTIONS}
              onChange={value => setStatus(value as MobileTaskStatus)}
              placeholder="精确任务状态"
            />}
            {!analysisOnly && <Select
              value={reviewStage}
              options={[
                { label: '全部复核', value: 'all' },
                { label: '等待研判', value: 'waiting_analysis' },
                { label: '已研判', value: 'analyzed' },
              ]}
              onChange={value => setReviewStage(value as MobileTaskReviewStage)}
              placeholder="复核阶段"
            />}
            {!analysisOnly && <Select
              value={priority}
              options={PRIORITY_OPTIONS}
              onChange={value => setPriority(value as MobileTaskPriority)}
              placeholder="优先级"
            />}
            <Select
              value={sort}
              options={SORT_OPTIONS}
              onChange={value => setSort(value as MobileTaskSort)}
              placeholder="更新时间"
            />
            <Select
              mode="multiple"
              value={watchCategories}
              options={watchCategoryOptions.map(option => ({
                value: option.value,
                label: `${option.label}（${option.count}）`,
              }))}
              onChange={setWatchCategories}
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="人员标记分类"
            />
          </div>
          )}
        </>}
        meta={<><span>当前筛选共 {total} 条</span>{keywordInput && <button type="button" className="text-[var(--app-primary)]" onClick={() => setKeywordInput('')}>清除搜索</button>}</>}
        actions={<Button onClick={() => void load()} loading={loading}>刷新</Button>}
      />

      {error && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={() => void load()}>重试</Button>} />}
      {sourceMessage && <Alert type="warning" showIcon message={sourceMessage} />}

      {!analysisOnly && canBulkAssign && (
        <section className={`app-card mobile-task-bulk-toolbar${selectionMode ? ' is-sticky' : ''}`}>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type={selectionMode ? 'primary' : 'default'}
              size="small"
              icon={<CheckSquareOutlined />}
              disabled={!selectionMode && total === 0}
              onClick={() => selectionMode ? leaveSelectionMode() : setSelectionMode(true)}
            >
              {selectionMode ? '退出选择' : '选择'}
            </Button>
            {selectionMode && (
              <>
                <Button
                  size="small"
                  loading={selectingAll}
                  onClick={() => void selectAllFiltered()}
                >
                  全选当前筛选
                </Button>
                <Button size="small" onClick={clearSelection} disabled={!selectedCount}>
                  清空
                </Button>
                <span className="text-xs text-[var(--app-text-secondary)]">已选 {selectedCount} 条</span>
                <Button
                  type="primary"
                  size="small"
                  disabled={!selectedCount}
                  onClick={() => { setBulkMode('single'); setBulkProgress(null); setBulkOpen(true) }}
                >
                  指定分配
                </Button>
                <Button
                  size="small"
                  disabled={!selectedCount || !selectedCommunity || bulkInspectorOptions.length < 2}
                  onClick={() => { setBulkMode('balanced'); setBulkProgress(null); setBulkOpen(true) }}
                >
                  平均分配
                </Button>
              </>
            )}
          </div>
          <div className="mt-1 text-xs text-[var(--app-text-secondary)]">
            {selectionMode
              ? '选择任务进行多选；选中第一条后会锁定同一社区，只处理未分配任务。'
              : '点击“选择”进入多选模式，再选择任务进行批量分配。'}
          </div>
        </section>
      )}

      {loading ? (
        <div className="mobile-task-list"><div className="app-card p-4"><Skeleton active paragraph={{ rows: 3 }} /></div><div className="app-card p-4"><Skeleton active paragraph={{ rows: 3 }} /></div></div>
      ) : rows.length === 0 ? (
        <div className="app-card py-8"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有符合条件的任务" /></div>
      ) : (
        <>
          {taskDisplayMode === 'table' && (
            <div className="hidden md:block">
              <MobileTaskTable
                rows={rows}
                loading={loading}
                analysisMode={analysisOnly}
                selectionMode={selectionMode}
                selectedRowKeys={[...selectedRows]}
                canSelect={canSelectTask}
                onSelect={(task, selected) => toggleSelected(task.row_key, selected)}
                onOpen={openTask}
                onCopy={(value, label) => void copyValue(value, label)}
                onSaved={() => load(page, false, true)}
              />
            </div>
          )}
          <div className={`mobile-task-list${taskDisplayMode === 'table' ? ' mobile-task-list--table-fallback' : ''}`}>
          {rows.map(task => {
            const state = STATE_LABELS[task.state]
            const surfaceTone = mobileTaskSurfaceTone(task)
            const phoneOptions = mobileTaskPhoneOptions(task.summary.phone)
            const copyPhones = phoneOptions.length > 0
              ? phoneOptions
              : task.summary.phone ? [task.summary.phone] : []
            const primaryPhone = copyPhones[0] || ''
            const extraPhoneCount = Math.max(copyPhones.length - 1, 0)
            const isAssignable = isTaskAssignable(task)
            const canSelect = canSelectTask(task)
            const isSelected = selectedRows.has(task.row_key)
            const sourceTags = mobileTaskSourceTags(task.summary.source)
            const currentAddress = String(task.summary.current_address || '').trim()
            const originalAddress = String(task.summary.original_address || '').trim()
            const primaryAddress = currentAddress || originalAddress || task.summary.address
            const showOriginalAddress = Boolean(
              currentAddress
              && originalAddress
              && currentAddress !== originalAddress,
            )
            const deadline = formatMobileTaskDeadline(task.summary.deadline)
            const openOrSelectTask = () => {
              if (selectionMode) {
                if (!isAssignable) {
                  message.info('该任务当前不能参与批量分配')
                  return
                }
                toggleSelected(task.row_key, !isSelected)
                return
              }
              openTask(task)
            }
            return (
              <article
                key={task.row_key}
                role="button"
                tabIndex={0}
                aria-pressed={selectionMode ? isSelected : undefined}
                aria-disabled={selectionMode && !canSelect ? true : undefined}
                data-mobile-task-row-key={task.row_key}
                className={[
                  'mobile-task-item-card',
                  `mobile-task-item-card--tone-${surfaceTone}`,
                  selectionMode ? 'is-selection-mode' : '',
                  isSelected ? 'is-selected' : '',
                  selectionMode && !canSelect ? 'is-selection-disabled' : '',
                ].filter(Boolean).join(' ')}
                onClick={openOrSelectTask}
                onKeyDown={event => {
                  if (event.target !== event.currentTarget) return
                  if (event.key === 'Enter' || (selectionMode && event.key === ' ')) {
                    event.preventDefault()
                    openOrSelectTask()
                  }
                }}
              >
                <div className="mobile-task-item-card__body">
                  <div className="mobile-task-item-card__header">
                    <div className="mobile-task-item-card__header-main">
                      <div className="mobile-task-item-card__title-row">
                        <h2 title={task.summary.title}>{task.summary.title}</h2>
                      </div>
                    </div>
                    <Tag color={state.color} className="mobile-task-item-card__state">{state.text}</Tag>
                  </div>
                  {(task.needs_review
                    || task.review_stage === 'waiting_analysis'
                    || task.review_stage === 'analyzed'
                    || task.photo_fetched
                    || task.conflict
                    || task.source_count > 1
                    || task.pending_sync
                    || Boolean(task.watch_marks?.length)) && (
                    <div className="mobile-task-item-card__flags">
                      {task.needs_review && <Tag color="warning" icon={<ExclamationCircleOutlined />}>需复核</Tag>}
                      {task.review_stage === 'waiting_analysis' && <Tag color="volcano">等待研判</Tag>}
                      {task.review_stage === 'analyzed' && <Tag color="purple">已研判</Tag>}
                      {task.photo_fetched && <Tag color="green">已调照片</Tag>}
                      {(task.conflict || task.source_count > 1) && <Tag color="red">来源异常</Tag>}
                      {task.sync_state === 'conflict' && <Tag color="red">同步冲突</Tag>}
                      {task.sync_state === 'retry' && <Tag color="orange">同步重试</Tag>}
                      {task.sync_state === 'pending' && <Tag color="blue">待同步</Tag>}
                      {task.watch_marks?.map(mark => (
                        <Tag key={`${task.row_key}-${mark.category_id}`} color={mark.color}>{mark.name}</Tag>
                      ))}
                    </div>
                  )}
                  {(primaryPhone || task.summary.identity_number || primaryAddress) && (
                    <dl className="mobile-task-item-card__key-info">
                      {task.summary.identity_number && (
                        <div className="mobile-task-item-card__key-row mobile-task-item-card__key-row--identity">
                          <dt>身份证号</dt>
                          <dd className="mobile-task-item-card__identity">
                            <button
                              type="button"
                              className="mobile-task-copy-value"
                              title="点击复制身份证号"
                              aria-label="复制身份证号"
                              onClick={event => void copyCardValue(event, task.summary.identity_number, '身份证号')}
                              onKeyDown={event => event.stopPropagation()}
                            >
                              <span className="mobile-task-copy-value__text">{task.summary.identity_number}</span>
                              <CopyOutlined aria-hidden="true" />
                            </button>
                          </dd>
                        </div>
                      )}
                      {primaryPhone && (
                        <div className="mobile-task-item-card__key-row mobile-task-item-card__key-row--phone">
                          <dt>手机号</dt>
                          <dd>
                            <MobilePhonePicker
                              phones={copyPhones}
                              mode="copy"
                              label={(
                                <span className="mobile-task-item-card__phone-label">
                                  <span>{primaryPhone}</span>
                                  {extraPhoneCount > 0 && (
                                    <span className="mobile-task-item-card__phone-extra">+{extraPhoneCount}</span>
                                  )}
                                  <CopyOutlined aria-hidden="true" />
                                </span>
                              )}
                              className="mobile-phone-native-select--card"
                              buttonProps={{
                                type: 'text',
                                className: 'mobile-task-item-card__phone-copy',
                              }}
                              onSelect={phone => void copyValue(phone, '手机号')}
                            />
                          </dd>
                        </div>
                      )}
                      {primaryAddress && (
                        <div className="mobile-task-item-card__key-row mobile-task-item-card__key-row--address">
                          <dt>{currentAddress ? '现住址' : '地址'}</dt>
                          <dd className="mobile-task-item-card__key-text" title={primaryAddress}>{primaryAddress}</dd>
                        </div>
                      )}
                      {showOriginalAddress && (
                        <div className="mobile-task-item-card__key-row mobile-task-item-card__key-row--old-address">
                          <dt>原地址</dt>
                          <dd title={originalAddress}>{originalAddress}</dd>
                        </div>
                      )}
                    </dl>
                  )}
                  {task.review_stage === 'analyzed' && task.summary.analysis && (
                    <div className="mobile-task-analysis">
                      <div className="mobile-task-analysis__label">研判结果</div>
                      <div className="mobile-task-analysis__value">{task.summary.analysis}</div>
                    </div>
                  )}
                  {sourceTags.length > 0 && (
                    <div className="mobile-task-source-cloud mobile-task-source-cloud--card">
                      <div>
                        {sourceTags.map(tag => (
                          <Tag key={`${task.row_key}-${tag}`} className="mobile-task-source-cloud__tag">{tag}</Tag>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
                <div className="mobile-task-item-card__footer">
                  <div className="mobile-task-item-card__footer-meta">
                    <div className="mobile-task-item-card__ownership">
                      <span title={task.community || '社区未填写'}>{task.community || '社区未填写'}</span>
                      <span aria-hidden="true">·</span>
                      <span title={task.inspector || '待分配'}>{task.inspector || '待分配'}</span>
                    </div>
                    <div className="mobile-task-item-card__date">
                      {deadline
                        ? `截止 ${deadline}`
                        : (task.source_count > 1 ? `${task.source_count} 条腾讯来源` : '点击进入处理')}
                    </div>
                  </div>
                  <MobilePhonePicker
                    phones={phoneOptions}
                    mode="dial"
                    label={phoneOptions.length > 1 ? '选择拨打' : '拨打'}
                    className="mobile-phone-native-select--compact"
                    buttonProps={{
                      type: 'primary',
                      ghost: true,
                      className: 'mobile-task-item-card__dial shrink-0',
                      icon: <PhoneOutlined />,
                    }}
                    onSelect={phone => void dial(phone)}
                  />
                </div>
              </article>
            )
          })}
          </div>
          {rows.length < total && (
            <div ref={loadMoreRef} className="mobile-task-load-more min-h-11" aria-live="polite">
              {loadingMore ? '正在加载更多任务…' : '继续下滑加载更多'}
              <Button type="link" onClick={() => void load(page + 1, true)} loading={loadingMore}>手动加载</Button>
            </div>
          )}
        </>
      )}

      <Modal
        open={bulkOpen}
        title={bulkMode === 'balanced' ? '平均分配核查人' : '指定分配核查人'}
        okText={bulkProgress?.error
          ? '继续分配'
          : bulkMode === 'balanced' ? '确认平均分配' : '确认分配'}
        cancelText="取消"
        confirmLoading={bulkSaving}
        okButtonProps={{
          disabled: !selectedCount
            || !selectedCommunity
            || (bulkMode === 'single' && !bulkInspector),
        }}
        onOk={() => void submitBulkAssignment()}
        onCancel={() => {
          if (!bulkSaving) {
            setBulkOpen(false)
            setBulkProgress(null)
          }
        }}
      >
        <div className="space-y-3 text-sm">
          {selectedCommunity ? (
            <p>
              {bulkMode === 'balanced'
                ? `将把当前选中的 ${selectedCount} 条${selectedCommunity}任务，尽量平均分给该社区的 ${bulkInspectorOptions.length} 名在岗组员。`
                : `将把当前选中的 ${selectedCount} 条${selectedCommunity}任务分配给该社区的一名在岗组员。`}
            </p>
          ) : (
            <Alert type="warning" showIcon message="批量分配必须一次只选择同一社区的任务" />
          )}
          {bulkMode === 'single' ? (
            <Select
              className="w-full"
              size="large"
              showSearch
              optionFilterProp="label"
              placeholder="请选择核查人"
              value={bulkInspector}
              options={bulkInspectorOptions.map(value => ({ value, label: value }))}
              onChange={value => {
                setBulkInspector(value)
                setBulkProgress(null)
              }}
            />
          ) : (
            <div className="mobile-task-balanced-preview">
              {bulkInspectorOptions.map((name, index) => {
                const base = Math.floor(selectedCount / bulkInspectorOptions.length)
                const remainder = selectedCount % bulkInspectorOptions.length
                const count = base + (index < remainder ? 1 : 0)
                return (
                  <span key={name}>{name}<strong>{count} 条</strong></span>
                )
              })}
            </div>
          )}
          {bulkProgress && (
            <div className="space-y-2 rounded border border-[var(--app-border)] bg-[var(--app-surface-muted)] p-3">
              <Progress
                percent={Math.round((bulkProgress.processed / bulkProgress.total) * 100)}
                status={bulkProgress.error ? 'exception' : bulkSaving ? 'active' : 'normal'}
              />
              <p className="text-xs text-[var(--app-text-secondary)]">
                已处理 {bulkProgress.processed}/{bulkProgress.total} 条；确认写入 {bulkProgress.updated} 条，跳过 {bulkProgress.skipped} 条{bulkProgress.failed ? `，失败 ${bulkProgress.failed} 条` : ''}。
              </p>
              {bulkProgress.details.length > 0 && (
                <p className="text-xs text-[var(--app-text-secondary)]">
                  跳过原因：{bulkSkipSummary(bulkProgress.details)}
                </p>
              )}
              {bulkProgress.failedDetails.length > 0 && (
                <p className="text-xs text-[var(--app-danger)]">
                  失败原因：{bulkSkipSummary(bulkProgress.failedDetails)}
                </p>
              )}
              {bulkProgress.error && (
                <Alert
                  type="warning"
                  showIcon
                  message="本次分配已中断"
                  description={`${bulkProgress.error}。点击“继续分配”会从当前分块续传，已经成功的任务不会被覆盖。`}
                />
              )}
            </div>
          )}
          <p className="text-xs text-[var(--app-text-secondary)]">已有核查人的任务、已完成任务、来源冲突任务会被跳过，不会被覆盖。</p>
        </div>
      </Modal>
    </div>
  )
}
