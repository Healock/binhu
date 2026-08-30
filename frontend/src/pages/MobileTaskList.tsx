import {
  CopyOutlined,
  DownloadOutlined,
  ExclamationCircleOutlined,
  PhoneOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import { Alert, Button, Empty, Input, Modal, Progress, Segmented, Select, Skeleton, Tag, Upload, message } from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState, type SyntheticEvent } from 'react'
import { useNavigate, useNavigationType, useSearchParams } from 'react-router-dom'
import {
  getMobileTaskAnalysisFilterOptions,
  getMobileTaskFilterOptions,
  getLatestQmfStatusScan,
  exportMobileTaskAnalysis,
  exportMobileTasks,
  importMobileTaskAnalysis,
  listMobileTaskAnalysis,
  listMobileTasks,
  startQmfStatusScan,
  type MobileTaskFacets,
  type MobileTaskFilterOption,
  type MobileTaskItem,
  type MobileTaskPriority,
  type MobileTaskReviewStage,
  type MobileTaskScope,
  type MobileTaskSort,
  type MobileTaskStatus,
  type QmfFeedbackState,
  type QmfStatusScanRun,
} from '../api/client'
import { useAuth } from '../context/AuthContext'
import { canManageFullchainArchive, isFlowTaskElevated, MOBILE_TASK_TYPES, UNVERIFIABLE_ARCHIVE_TYPES } from '../utils/mobileTaskRouting'
import {
  formatMobileTaskDeadline,
  mobileTaskCanLaunchTelephone,
  mobileTaskCurrentAddressLabel,
  mobileTaskPhoneOptions,
  mobileTaskSourceTags,
  mobileTaskSurfaceTone,
  mobileTaskUsesRegistrationClosure,
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
import MobileTaskAssignmentWorkbench from '../components/MobileTaskAssignmentWorkbench'
import QmfFeedbackStatus, { QMF_FEEDBACK_OPTIONS } from '../components/QmfFeedbackStatus'
import ResidenceRegistrationStatus from '../components/ResidenceRegistrationStatus'
import RegistrationLinkStatus from '../components/RegistrationLinkStatus'
import UnverifiableReviewNotice from '../components/UnverifiableReviewNotice'
import FullchainArchivePanel from '../components/FullchainArchivePanel'
import { ListToolbar } from '../components/ui'
import useDebouncedValue from '../hooks/useDebouncedValue'
import useSystemTime from '../hooks/useSystemTime'
import { openNativePhoneDialer } from '../utils/nativePhone'
import { downloadBlob } from '../utils/fileDownload'

const MODEL_THREE_PARSER = '疑似未注销模型三'
const ALL_ANALYSIS_TYPES = '__all__'

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
  { label: '默认（状态 + 地址）', value: 'priority' },
  { label: '地址升序', value: 'address_asc' },
  { label: '身份证号升序', value: 'identity_asc' },
  { label: '最近更新', value: 'updated_desc' },
  { label: '最早更新', value: 'updated_asc' },
] satisfies Array<{ label: string; value: MobileTaskSort }>

const STATE_LABELS = {
  unchecked: { text: '未核查', color: 'gold' },
  checked: { text: '待补结果', color: 'orange' },
  completed: { text: '已完成', color: 'green' },
} as const

const PRIORITY_CARDS: Array<{ key: MobileTaskPriority; label: string }> = [
  { key: 'all', label: '全部' },
  { key: 'ordinary', label: '普通待处理' },
  { key: 'waiting_analysis', label: '等待研判' },
  { key: 'analyzed', label: '已研判' },
  { key: 'source_exception', label: '来源异常' },
  { key: 'completed', label: '已完成' },
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
  registration_review_count: 0,
  qmf_feedback_counts: {
    not_scanned: 0,
    stale: 0,
    pending: 0,
    completed_match: 0,
    completed_mismatch: 0,
    not_found: 0,
    non_jurisdiction: 0,
    error: 0,
  },
}

const EMPTY_ASSIGNMENT = {
  enabled: false,
  community_aliases: {} as Record<string, string>,
  inspectors_by_community: {} as Record<string, string[]>,
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

function readQmfFeedbackStates(searchParams: URLSearchParams): QmfFeedbackState[] {
  const valid = new Set(QMF_FEEDBACK_OPTIONS.map(option => option.value))
  return readMulti(searchParams, 'qmf_state')
    .filter((value): value is QmfFeedbackState => valid.has(value as QmfFeedbackState))
}

function ensureTaskKey(task: MobileTaskItem): MobileTaskItem {
  return task.task_key
    ? task
    : { ...task, task_key: `${task.parser_type}:${task.row_key}` }
}

export default function MobileTaskList({
  mode = 'tasks',
  onAnalysisCountChange,
  manageUrl = true,
}: {
  mode?: 'tasks' | 'analysis'
  onAnalysisCountChange?: (count: number) => void
  manageUrl?: boolean
}) {
  const navigate = useNavigate()
  const navigationType = useNavigationType()
  const { recordActivity, user } = useAuth()
  const formatSystemTime = useSystemTime()
  const [searchParams, setSearchParams] = useSearchParams()
  const analysisOnly = mode === 'analysis'
  const requestedType = searchParams.get('type') || MOBILE_TASK_TYPES[0]
  const parserType = MOBILE_TASK_TYPES.includes(requestedType as any)
    ? requestedType
    : MOBILE_TASK_TYPES[0]
  const [analysisParserSelection, setAnalysisParserSelection] = useState<string[]>(() => {
    if (!analysisOnly) return []
    const requested = readMulti(searchParams, 'type')
    if (requested.includes(ALL_ANALYSIS_TYPES)) return [ALL_ANALYSIS_TYPES]
    const valid = requested.filter(value => MOBILE_TASK_TYPES.includes(value as any))
    return valid.length ? valid : [ALL_ANALYSIS_TYPES]
  })
  const analysisParserTypes = useMemo(
    () => analysisParserSelection.includes(ALL_ANALYSIS_TYPES)
      ? [...MOBILE_TASK_TYPES]
      : analysisParserSelection,
    [analysisParserSelection],
  )
  const requestedScope = searchParams.get('scope')
  const adminMode = isFlowTaskElevated(
    user?.member?.position,
    user?.role,
    user?.permission_groups?.map(group => group.code),
    user?.permissions,
  )
  const scope: MobileTaskScope = analysisOnly || adminMode
    ? 'all'
    : requestedScope === 'community' ? 'community' : 'mine'
  const requestedStatus = searchParams.get('status')
  const requestedReviewStage = searchParams.get('review_stage')
  const selectableReviewStages: MobileTaskReviewStage[] = [
    'waiting_analysis',
    'analyzed',
    'initial_pending',
    'initial_extension',
    'deep_pending',
    'deep_extension',
  ]
  const initialQmfFeedbackStates = readQmfFeedbackStates(searchParams)
  const [status, setStatus] = useState<MobileTaskStatus>(
    analysisOnly
      ? 'all'
      : initialQmfFeedbackStates.length
      ? 'completed'
      : ['pending', 'unchecked', 'checked', 'review', 'registration_review', 'completed', 'all'].includes(requestedStatus || '')
      ? requestedStatus as MobileTaskStatus
      : 'all',
  )
  const [reviewStage, setReviewStage] = useState<MobileTaskReviewStage>(
    analysisOnly
      ? selectableReviewStages.includes(requestedReviewStage as MobileTaskReviewStage)
        ? requestedReviewStage as MobileTaskReviewStage
        : 'initial_pending'
      : ['waiting_analysis', 'analyzed'].includes(requestedReviewStage || '')
      ? requestedReviewStage as MobileTaskReviewStage
      : 'all',
  )
  const [communities, setCommunities] = useState<string[]>(readMulti(searchParams, 'community'))
  const [inspectors, setInspectors] = useState<string[]>(readMulti(searchParams, 'inspector'))
  const [watchCategories, setWatchCategories] = useState<number[]>(readMultiNumber(searchParams, 'watch_category'))
  const [qmfFeedbackStates, setQmfFeedbackStates] = useState<QmfFeedbackState[]>(
    initialQmfFeedbackStates,
  )
  const [priority, setPriority] = useState<MobileTaskPriority>(readPriority(searchParams.get('priority')))
  const [sort, setSort] = useState<MobileTaskSort>(readSort(searchParams.get('sort')))
  const taskDisplayMode = user?.task_display_mode || 'table'
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
  const [rows, setRows] = useState<MobileTaskItem[]>(() => (
    snapshotRef.current?.rows.map(ensureTaskKey) || []
  ))
  const [total, setTotal] = useState(() => snapshotRef.current?.total || 0)
  const [page, setPage] = useState(() => snapshotRef.current?.page || 1)
  const [loading, setLoading] = useState(() => !snapshotRef.current)
  const [loadingMore, setLoadingMore] = useState(false)
  const [optionsLoading, setOptionsLoading] = useState(false)
  const [moreOpen, setMoreOpen] = useState(false)
  const [error, setError] = useState('')
  const [sourceMessage, setSourceMessage] = useState(() => snapshotRef.current?.source_message || '')
  const [qmfScan, setQmfScan] = useState<QmfStatusScanRun | null>(null)
  const [qmfScanLoading, setQmfScanLoading] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [importingAnalysis, setImportingAnalysis] = useState(false)
  const [assignmentWorkbenchOpen, setAssignmentWorkbenchOpen] = useState(false)
  const loadingMoreRef = useRef(false)
  const scrollLoadArmedRef = useRef(false)
  const lastScrollTopRef = useRef(0)
  const optionsRequestId = useRef(0)
  const listRequestId = useRef(0)
  const loadedPageRef = useRef(snapshotRef.current?.loaded_page || 1)
  const canBulkAssign = assignment.enabled
  const isModelThree = parserType === MODEL_THREE_PARSER && !analysisOnly
  const canStartQmfScan = Boolean(user?.permissions.includes('qmf.registration.execute'))
  const canUseFullchainArchive = Boolean(
    !analysisOnly
      && UNVERIFIABLE_ARCHIVE_TYPES.has(parserType)
      && canManageFullchainArchive(
        user?.member?.position,
        user?.role,
        user?.permission_groups?.map(group => group.code),
        user?.permissions,
        user?.permission_scopes?.['police.dispatch.manage'],
        user?.data_scope,
      ),
  )

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
      row_key: task.task_key,
      saved_at: Date.now(),
    })
    navigate(`${analysisOnly ? '/police-analysis' : '/tasks'}/${encodeURIComponent(task.parser_type)}/${task.row_key}?scope=${scope}`)
  }, [analysisOnly, facets, keywordInput, mode, navigate, page, rows, scope, sourceMessage, taskDisplayMode, total])

  const loadOptions = useCallback(async () => {
    const requestId = ++optionsRequestId.current
    setOptionsLoading(true)
    try {
      const result = analysisOnly
        ? await getMobileTaskAnalysisFilterOptions(
          analysisParserTypes,
          communities,
          reviewStage,
        )
        : await getMobileTaskFilterOptions(
          parserType,
          scope,
          communities,
          'all',
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
  }, [analysisOnly, analysisParserTypes, communities, parserType, reviewStage, scope])

  useEffect(() => { void loadOptions() }, [loadOptions])

  useEffect(() => {
    if (analysisOnly) onAnalysisCountChange?.(facets.total)
  }, [analysisOnly, facets.total, onAnalysisCountChange])

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
      const requestPage = (requestedPage: number) => analysisOnly
        ? listMobileTaskAnalysis({
          parser_types: analysisParserTypes,
          scope: 'all',
          review_stage: reviewStage,
          communities,
          inspectors,
          watch_categories: watchCategories,
          sort,
          keyword: keyword || undefined,
          page: requestedPage,
          page_size: 50,
        })
        : listMobileTasks({
          parser_type: parserType,
          scope,
          status,
          review_stage: reviewStage,
          communities,
          inspectors,
          watch_categories: watchCategories,
          qmf_feedback_states: qmfFeedbackStates,
          priority,
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
  }, [analysisOnly, analysisParserTypes, communities, inspectors, keyword, parserType, priority, qmfFeedbackStates, reviewStage, scope, sort, status, watchCategories])

  const loadQmfScan = useCallback(async (silent = true) => {
    if (!isModelThree) {
      setQmfScan(null)
      return null
    }
    if (!silent) setQmfScanLoading(true)
    try {
      const result = await getLatestQmfStatusScan()
      setQmfScan(result)
      return result
    } catch (reason: any) {
      if (!silent) message.error(reason?.response?.data?.detail || '全民防反馈扫描进度读取失败')
      return null
    } finally {
      if (!silent) setQmfScanLoading(false)
    }
  }, [isModelThree])

  useEffect(() => {
    if (!isModelThree) {
      setQmfScan(null)
      return undefined
    }
    void loadQmfScan()
    const timer = window.setInterval(async () => {
      const previousActive = qmfScan?.status === 'queued' || qmfScan?.status === 'running'
      const result = await loadQmfScan()
      const active = result?.status === 'queued' || result?.status === 'running'
      if (previousActive && !active) await load(1, false, true)
    }, qmfScan?.status === 'queued' || qmfScan?.status === 'running' ? 3_000 : 30_000)
    return () => window.clearInterval(timer)
  }, [isModelThree, load, loadQmfScan, qmfScan?.status])

  const confirmStartQmfScan = () => {
    Modal.confirm({
      title: '全量核对全民防反馈',
      content: '将冻结当前全部已完成模型三任务清单，并使用四路并发逐条只读核对。扫描不会修改任何业务数据。',
      okText: '开始核对',
      cancelText: '取消',
      onOk: async () => {
        setQmfScanLoading(true)
        try {
          const result = await startQmfStatusScan()
          setQmfScan(result)
          message.success(result.total_count ? `已加入 ${result.total_count} 条任务` : '当前没有需要扫描的已完成任务')
          if (!result.total_count) await load(1, false, true)
        } catch (reason: any) {
          message.error(reason?.response?.data?.detail || '全民防反馈扫描启动失败')
          throw reason
        } finally {
          setQmfScanLoading(false)
        }
      },
    })
  }

  useEffect(() => {
    const scrollContainer = pageRootRef.current?.closest('main')
    if (!(scrollContainer instanceof HTMLElement) || loading || loadingMore || rows.length >= total) return undefined
    lastScrollTopRef.current = scrollContainer.scrollTop
    const armScrollLoad = () => { scrollLoadArmedRef.current = true }
    const armWheelLoad = (event: WheelEvent) => { if (event.deltaY > 0) armScrollLoad() }
    const armKeyLoad = (event: KeyboardEvent) => {
      if (['ArrowDown', 'PageDown', 'End', ' '].includes(event.key)) armScrollLoad()
    }
    const handleScroll = () => {
      const nextScrollTop = scrollContainer.scrollTop
      const movingDown = nextScrollTop > lastScrollTopRef.current
      lastScrollTopRef.current = nextScrollTop
      if (!movingDown || !scrollLoadArmedRef.current || loadingMoreRef.current) return
      const remaining = scrollContainer.scrollHeight - nextScrollTop - scrollContainer.clientHeight
      if (remaining > 80) return
      scrollLoadArmedRef.current = false
      void load(page + 1, true)
    }
    scrollContainer.addEventListener('wheel', armWheelLoad, { passive: true })
    scrollContainer.addEventListener('touchstart', armScrollLoad, { passive: true })
    scrollContainer.addEventListener('pointerdown', armScrollLoad, { passive: true })
    scrollContainer.addEventListener('scroll', handleScroll, { passive: true })
    window.addEventListener('keydown', armKeyLoad)
    return () => {
      scrollContainer.removeEventListener('wheel', armWheelLoad)
      scrollContainer.removeEventListener('touchstart', armScrollLoad)
      scrollContainer.removeEventListener('pointerdown', armScrollLoad)
      scrollContainer.removeEventListener('scroll', handleScroll)
      window.removeEventListener('keydown', armKeyLoad)
    }
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
    if (!manageUrl) return
    const next = new URLSearchParams()
    if (analysisOnly) {
      analysisParserSelection.forEach(value => next.append('type', value))
    } else {
      next.set('type', parserType)
    }
    next.set('scope', analysisOnly ? 'all' : scope)
    next.set('status', analysisOnly ? 'all' : status)
    if (analysisOnly) next.set('review_stage', reviewStage)
    else if (reviewStage !== 'all') next.set('review_stage', reviewStage)
    communities.forEach(value => next.append('community', value))
    inspectors.forEach(value => next.append('inspector', value))
    watchCategories.forEach(value => next.append('watch_category', String(value)))
    if (isModelThree) qmfFeedbackStates.forEach(value => next.append('qmf_state', value))
    if (!analysisOnly && priority !== 'all') next.set('priority', priority)
    if (sort !== 'priority') next.set('sort', sort)
    setSearchParams(next, { replace: true })
  }, [analysisOnly, analysisParserSelection, communities, inspectors, isModelThree, manageUrl, parserType, priority, qmfFeedbackStates, reviewStage, scope, setSearchParams, sort, status, watchCategories])

  const updateQuery = (type: string, nextScope: MobileTaskScope) => {
    const next = new URLSearchParams()
    next.set('type', type)
    next.set('scope', nextScope)
    next.set('status', 'all')
    setCommunities([])
    setInspectors([])
    setWatchCategories([])
    setQmfFeedbackStates([])
    setPriority('all')
    setSort('priority')
    setStatus('all')
    setReviewStage(analysisOnly ? 'waiting_analysis' : 'all')
    setSearchParams(next)
  }

  const clearFilters = () => {
    if (analysisOnly) setAnalysisParserSelection([ALL_ANALYSIS_TYPES])
    setCommunities([])
    setInspectors([])
    setWatchCategories([])
    setQmfFeedbackStates([])
    setPriority('all')
    setSort('priority')
    setStatus('all')
    setReviewStage(analysisOnly ? 'waiting_analysis' : 'all')
    setKeywordInput('')
  }

  const updateAnalysisParserSelection = (values: string[]) => {
    let next = values.filter(value => (
      value === ALL_ANALYSIS_TYPES || MOBILE_TASK_TYPES.includes(value as any)
    ))
    if (next.includes(ALL_ANALYSIS_TYPES)) {
      next = analysisParserSelection.includes(ALL_ANALYSIS_TYPES) && next.length > 1
        ? next.filter(value => value !== ALL_ANALYSIS_TYPES)
        : [ALL_ANALYSIS_TYPES]
    }
    if (!next.length) next = [ALL_ANALYSIS_TYPES]
    setAnalysisParserSelection(next)
    setCommunities([])
    setInspectors([])
    setWatchCategories([])
    setPage(1)
  }

  const selectQmfFeedbackResult = (state: QmfFeedbackState | 'all') => {
    setQmfFeedbackStates(state === 'all' ? [] : [state])
    setStatus('completed')
    setPriority('all')
    setReviewStage('all')
    setPage(1)
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
    || (isModelThree && qmfFeedbackStates.length > 0)
    || (analysisOnly && !analysisParserSelection.includes(ALL_ANALYSIS_TYPES))
    || (!analysisOnly && priority !== 'all')
    || (!analysisOnly && status !== 'all')
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
    try {
      if (await openNativePhoneDialer(phone)) return
    } catch {
      await navigator.clipboard.writeText(phone).catch(() => {})
      message.error('无法打开系统拨号界面，电话号码已复制')
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

  const exportCurrent = async () => {
    setExporting(true)
    try {
      const blob = analysisOnly
        ? await exportMobileTaskAnalysis({
          parser_types: analysisParserTypes,
          scope: 'all',
          review_stage: reviewStage,
          communities,
          inspectors,
          watch_categories: watchCategories,
          sort,
          keyword: keyword || undefined,
        })
        : await exportMobileTasks({
          parser_type: parserType,
          scope,
          status,
          review_stage: reviewStage,
          communities,
          inspectors,
          watch_categories: watchCategories,
          qmf_feedback_states: qmfFeedbackStates,
          priority,
          sort,
          keyword: keyword || undefined,
        })
      const saved = await downloadBlob(
        blob,
        `${analysisOnly ? '研判任务' : parserType}-${new Date().toISOString().slice(0, 10)}.xlsx`,
      )
      if (saved) message.success(`已导出 ${analysisOnly ? '研判任务' : '当前筛选结果'}`)
    } catch (reason: any) {
      let detail = reason?.response?.data?.detail
      if (reason?.response?.data instanceof Blob) {
        try { detail = JSON.parse(await reason.response.data.text())?.detail } catch { detail = '' }
      }
      message.error(detail || '导出失败')
    } finally {
      setExporting(false)
    }
  }

  const importAnalysis = async (file: File) => {
    setImportingAnalysis(true)
    try {
      const result = await importMobileTaskAnalysis(file)
      if (result.failed_count) {
        message.warning(`已导入 ${result.success_count} 条，${result.failed_count} 条需要处理`)
      } else {
        message.success(`已导入 ${result.success_count} 条研判结果`)
      }
      await load(1, false, true)
    } catch (reason: any) {
      let detail = reason?.response?.data?.detail
      if (reason?.response?.data instanceof Blob) {
        try { detail = JSON.parse(await reason.response.data.text())?.detail } catch { detail = '' }
      }
      message.error(detail || '研判文件导入失败')
    } finally {
      setImportingAnalysis(false)
    }
  }

  return (
    <div ref={pageRootRef} className="mobile-task-page">
      <ListToolbar
        className="mobile-task-filter-card"
        filters={<div className="mobile-task-filter-grid">
          {analysisOnly ? <Select
            mode="multiple"
            size="large"
            value={analysisParserSelection}
            maxTagCount="responsive"
            optionFilterProp="label"
            onChange={updateAnalysisParserSelection}
            options={[
              { value: ALL_ANALYSIS_TYPES, label: '全部数据' },
              ...MOBILE_TASK_TYPES.map(value => ({ value, label: value })),
            ]}
          /> : <Select
            size="large"
            value={parserType}
            onChange={value => updateQuery(value, scope)}
            options={MOBILE_TASK_TYPES.map(value => ({ value, label: value }))}
          />}
          {analysisOnly || adminMode ? (
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
          <div className={`mobile-task-priority-grid${analysisOnly ? ' mobile-task-analysis-stage-grid' : ''}`} aria-label={analysisOnly ? '研判阶段筛选' : '任务快捷筛选'}>
            {(analysisOnly
              ? [
                { key: 'initial_pending', label: '初步待研判', count: facets.review_stage_counts?.initial_pending || 0 },
                { key: 'initial_extension', label: '初步复核中', count: facets.review_stage_counts?.initial_extension || 0 },
                { key: 'deep_pending', label: '深度待研判', count: facets.review_stage_counts?.deep_pending || 0 },
                { key: 'deep_extension', label: '深度复核中', count: facets.review_stage_counts?.deep_extension || 0 },
                { key: 'all', label: '全部', count: facets.total },
              ]
              : PRIORITY_CARDS.map(card => ({
                key: card.key,
                label: card.label,
                count: card.key === 'all' ? facets.total : facets.priority_counts[card.key],
              }))
            ).map(card => {
              const active = analysisOnly
                ? (card.key === 'all' ? reviewStage === 'all' : reviewStage === card.key)
                : (card.key === 'all' ? priority === 'all' && status === 'all' : priority === card.key)
              return (
                <button
                  key={card.key}
                  type="button"
                  className={`mobile-task-priority-card${active ? ' is-active' : ''}`}
                  onClick={() => {
                    if (analysisOnly) {
                      setReviewStage(card.key === 'all' ? 'all' : card.key as MobileTaskReviewStage)
                      setPage(1)
                    } else {
                      selectPriorityCard(card.key as MobileTaskPriority)
                    }
                  }}
                >
                  <span>{card.label}</span>
                  <strong>{card.count}</strong>
                </button>
              )
            })}
          </div>

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
              options={[
                ...STATUS_OPTIONS.slice(0, 3),
                ...(mobileTaskUsesRegistrationClosure(parserType)
                  ? [{
                      label: `登记复核（${facets.registration_review_count}）`,
                      value: 'registration_review' as MobileTaskStatus,
                    }]
                  : []),
                ...STATUS_OPTIONS.slice(3),
              ]}
              onChange={value => setStatus(value as MobileTaskStatus)}
              placeholder="精确任务状态"
            />}
            {!analysisOnly && <Select
              value={reviewStage}
              options={[
                { label: '全部复核', value: 'all' },
                { label: '初步待研判', value: 'initial_pending' },
                { label: '初步复核中', value: 'initial_extension' },
                { label: '深度待研判', value: 'deep_pending' },
                { label: '深度复核中', value: 'deep_extension' },
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
              placeholder="排序方式"
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
              placeholder="人员标签分类"
            />
            {isModelThree && <Select
              mode="multiple"
              value={qmfFeedbackStates}
              options={QMF_FEEDBACK_OPTIONS.map(option => ({
                value: option.value,
                label: `${option.label}（${facets.qmf_feedback_counts[option.value] || 0}）`,
              }))}
              onChange={values => {
                const next = values as QmfFeedbackState[]
                setQmfFeedbackStates(next)
                if (next.length) {
                  setStatus('completed')
                  setPriority('all')
                  setReviewStage('all')
                }
              }}
              allowClear
              maxTagCount="responsive"
              placeholder="全民防反馈状态"
            />}
          </div>
          )}
        </>}
        meta={<><span>当前筛选共 {total} 条</span>{keywordInput && <button type="button" className="text-[var(--app-primary)]" onClick={() => setKeywordInput('')}>清除搜索</button>}</>}
        actions={<>
          <Button onClick={() => void load()} loading={loading}>刷新</Button>
          <Button icon={<DownloadOutlined />} onClick={() => void exportCurrent()} loading={exporting}>
            {analysisOnly ? '导出待研判' : '导出当前结果'}
          </Button>
          {analysisOnly && <Upload
            accept=".xlsx"
            showUploadList={false}
            beforeUpload={file => { void importAnalysis(file); return false }}
          >
            <Button loading={importingAnalysis}>导入研判结果</Button>
          </Upload>}
          {isModelThree && canStartQmfScan && (
            <Button
              type="primary"
              loading={qmfScanLoading}
              disabled={qmfScan?.status === 'queued' || qmfScan?.status === 'running'}
              onClick={confirmStartQmfScan}
            >
              全量核对全民防反馈
            </Button>
          )}
        </>}
      />

      {isModelThree && qmfScan && (
        <section className="app-card grid gap-3 p-4" aria-live="polite">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <strong className="text-[var(--app-text-strong)]">
                {qmfScan.scan_mode === 'full' ? '全民防全量核对' : '全民防每日增量核对'}
              </strong>
              <span className="ml-2 text-xs text-[var(--app-text-secondary)]">
                {qmfScan.status === 'queued' ? '等待开始' : qmfScan.status === 'running' ? '正在核对' : qmfScan.status === 'completed' ? '已完成' : qmfScan.status === 'partial' ? '部分完成' : '已停止'}
              </span>
            </div>
            <span className="text-xs text-[var(--app-text-secondary)]">
              {qmfScan.finished_at
                ? `完成于 ${formatSystemTime(qmfScan.finished_at)}`
                : qmfScan.started_at
                  ? `开始于 ${formatSystemTime(qmfScan.started_at)}`
                  : `创建于 ${formatSystemTime(qmfScan.created_at)}`}
            </span>
          </div>
          <Progress
            percent={qmfScan.total_count
              ? Math.min(100, Math.round((qmfScan.processed_count / qmfScan.total_count) * 100))
              : 100}
            status={qmfScan.status === 'failed' ? 'exception' : qmfScan.status === 'running' ? 'active' : 'normal'}
          />
          <div className="qmf-scan-result-filters" aria-label="全民防核对结果筛选">
            {[
              { state: 'all' as const, label: '已处理', count: `${qmfScan.processed_count}/${qmfScan.total_count}` },
              { state: 'completed_match' as const, label: '一致', count: qmfScan.match_count },
              { state: 'completed_mismatch' as const, label: '不一致', count: qmfScan.mismatch_count, tone: 'danger' },
              { state: 'pending' as const, label: '未核查', count: qmfScan.pending_count },
              { state: 'not_found' as const, label: '无记录', count: qmfScan.not_found_count },
              { state: 'non_jurisdiction' as const, label: '非本辖区', count: qmfScan.non_jurisdiction_count },
              { state: 'error' as const, label: '异常', count: qmfScan.error_count, tone: 'warning' },
            ].map(item => {
              const active = item.state === 'all'
                ? status === 'completed' && qmfFeedbackStates.length === 0
                : qmfFeedbackStates.length === 1 && qmfFeedbackStates[0] === item.state
              return (
                <button
                  key={item.state}
                  type="button"
                  className={`qmf-scan-result-filter${active ? ' is-active' : ''}${item.tone ? ` is-${item.tone}` : ''}`}
                  aria-pressed={active}
                  onClick={() => selectQmfFeedbackResult(item.state)}
                >
                  <span>{item.label}</span>
                  <strong>{item.count}</strong>
                </button>
              )
            })}
          </div>
        </section>
      )}

      {canUseFullchainArchive && <FullchainArchivePanel parserType={parserType} />}

      {error && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={() => void load()}>重试</Button>} />}
      {sourceMessage && <Alert type="warning" showIcon message={sourceMessage} />}

      {!analysisOnly && canBulkAssign && (
        <section className="app-card mobile-task-bulk-toolbar">
          <Button type="primary" onClick={() => setAssignmentWorkbenchOpen(true)}>
            分配数据
          </Button>
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
                canClaimUnassigned={!analysisOnly && user?.member?.position === '组员'}
                selectionMode={false}
                selectedRowKeys={[]}
                canSelect={() => false}
                onSelect={() => undefined}
                onOpen={openTask}
                onCopy={(value, label) => void copyValue(value, label)}
                sort={sort}
                onSortChange={setSort}
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
            const sourceTags = task.parser_type === '全链条'
              ? mobileTaskSourceTags(task.summary.source)
              : []
            const currentAddress = String(task.summary.current_address || '').trim()
            const originalAddress = String(task.summary.original_address || '').trim()
            const primaryAddress = currentAddress || originalAddress || task.summary.address
            const showOriginalAddress = Boolean(
              currentAddress
              && originalAddress
              && currentAddress !== originalAddress,
            )
            const deadline = formatMobileTaskDeadline(task.summary.deadline)
            return (
              <article
                key={task.task_key}
                role="button"
                tabIndex={0}
                data-mobile-task-row-key={task.task_key}
                className={[
                  'mobile-task-item-card',
                  `mobile-task-item-card--tone-${surfaceTone}`,
                ].filter(Boolean).join(' ')}
                onClick={() => openTask(task)}
                onKeyDown={event => {
                  if (event.target !== event.currentTarget) return
                  if (event.key === 'Enter') {
                    event.preventDefault()
                    openTask(task)
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
                    || task.review_stage === 'initial_pending'
                    || task.review_stage === 'initial_extension'
                    || task.review_stage === 'deep_pending'
                    || task.review_stage === 'deep_extension'
                    || task.review_stage === 'final_unverifiable'
                    || task.review_stage === 'source_exception'
                    || task.conflict
                    || task.source_count > 1
                    || task.pending_sync
                    || Boolean(task.watch_marks?.length)
                    || Boolean(task.qmf_status)
                    || Boolean(task.registration_link)
                    || task.residence_status?.state === 'first_registration') && (
                    <div className="mobile-task-item-card__flags">
                      {task.needs_review && <Tag color="warning" icon={<ExclamationCircleOutlined />}>需复核</Tag>}
                      {task.review_stage === 'waiting_analysis' && <Tag color="volcano">等待研判</Tag>}
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
                          <dt>
                            {currentAddress
                              ? mobileTaskCurrentAddressLabel(task.parser_type, task.summary.result || '')
                              : '地址'}
                          </dt>
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
                  {task.review_flow && !['resolved', 'archived'].includes(task.review_flow.state) && (
                    <UnverifiableReviewNotice flow={task.review_flow} showStateLabel />
                  )}
                  {['analyzed', 'initial_extension', 'deep_pending', 'deep_extension'].includes(task.review_stage) && task.summary.analysis && (
                    <div className="mobile-task-analysis">
                      <div className="mobile-task-analysis__label">研判结果</div>
                      <div className="mobile-task-analysis__value">{task.summary.analysis}</div>
                    </div>
                  )}
                  {sourceTags.length > 0 && (
                    <div className="mobile-task-source-cloud mobile-task-source-cloud--card">
                      <div>
                        {sourceTags.map(tag => (
                          <Tag key={`${task.task_key}-${tag}`} className="mobile-task-source-cloud__tag">{tag}</Tag>
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
                        : (task.source_count > 1 ? `${task.source_count} 条本地来源` : '点击进入处理')}
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
            <div className="mobile-task-load-more min-h-11" aria-live="polite">
              {loadingMore
                ? '正在加载下一批任务…'
                : `已加载 ${rows.length} / ${total} 条，继续向下滑到底部加载下一批`}
            </div>
          )}
        </>
      )}

      <MobileTaskAssignmentWorkbench
        open={assignmentWorkbenchOpen}
        parserType={parserType}
        onClose={() => setAssignmentWorkbenchOpen(false)}
        onChanged={() => load(1)}
      />
    </div>
  )
}
