import {
  CheckSquareOutlined,
  CopyOutlined,
  ExclamationCircleOutlined,
  PhoneOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import { Alert, Button, Empty, Input, Modal, Segmented, Select, Skeleton, Tag, message } from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState, type SyntheticEvent } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  getMobileTaskFilterOptions,
  bulkAssignMobileTasks,
  listMobileTasks,
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
  mobileTaskCanLaunchTelephone,
  mobileTaskPhoneOptions,
  mobileTaskSourceTags,
} from '../utils/mobileTasks'
import MobilePhonePicker from '../components/MobilePhonePicker'

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
  { label: '待同步', value: 'pending_sync' },
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
  unchecked: { text: '未核查', color: 'red' },
  checked: { text: '待补结果', color: 'orange' },
  completed: { text: '已完成', color: 'green' },
} as const

const PRIORITY_CARDS: Array<{ key: MobileTaskPriority; label: string }> = [
  { key: 'analyzed', label: '已研判' },
  { key: 'source_exception', label: '来源异常' },
  { key: 'pending_sync', label: '待同步' },
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

export default function MobileTaskList() {
  const navigate = useNavigate()
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
  )
  const scope: MobileTaskScope = adminMode
    ? 'all'
    : requestedScope === 'community' ? 'community' : 'mine'
  const requestedStatus = searchParams.get('status')
  const requestedReviewStage = searchParams.get('review_stage')
  const [status, setStatus] = useState<MobileTaskStatus>(
    ['pending', 'unchecked', 'checked', 'review', 'completed', 'all'].includes(requestedStatus || '')
      ? requestedStatus as MobileTaskStatus
      : 'pending',
  )
  const [reviewStage, setReviewStage] = useState<MobileTaskReviewStage>(
    ['waiting_analysis', 'analyzed'].includes(requestedReviewStage || '')
      ? requestedReviewStage as MobileTaskReviewStage
      : 'all',
  )
  const [communities, setCommunities] = useState<string[]>(readMulti(searchParams, 'community'))
  const [inspectors, setInspectors] = useState<string[]>(readMulti(searchParams, 'inspector'))
  const [watchCategories, setWatchCategories] = useState<number[]>(readMultiNumber(searchParams, 'watch_category'))
  const [priority, setPriority] = useState<MobileTaskPriority>(readPriority(searchParams.get('priority')))
  const [sort, setSort] = useState<MobileTaskSort>(readSort(searchParams.get('sort')))
  const [keywordInput, setKeywordInput] = useState('')
  const [keyword, setKeyword] = useState('')
  const [communityOptions, setCommunityOptions] = useState<MobileTaskFilterOption[]>([])
  const [inspectorOptions, setInspectorOptions] = useState<MobileTaskFilterOption[]>([])
  const [assignment, setAssignment] = useState(EMPTY_ASSIGNMENT)
  const [watchCategoryOptions, setWatchCategoryOptions] = useState<Array<{ value: number; label: string; color: string; alert_level: string; count: number }>>([])
  const [facets, setFacets] = useState<MobileTaskFacets>(EMPTY_FACETS)
  const [rows, setRows] = useState<MobileTaskItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [optionsLoading, setOptionsLoading] = useState(false)
  const [moreOpen, setMoreOpen] = useState(false)
  const [error, setError] = useState('')
  const [sourceMessage, setSourceMessage] = useState('')
  const [selectionMode, setSelectionMode] = useState(false)
  const [selectedRows, setSelectedRows] = useState<Set<string>>(new Set())
  const [bulkOpen, setBulkOpen] = useState(false)
  const [bulkInspector, setBulkInspector] = useState<string | undefined>()
  const [bulkSaving, setBulkSaving] = useState(false)
  const optionsRequestId = useRef(0)
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
  const selectedCommunity = selectedCommunities.length === 1
    ? selectedCommunities[0]
    : ''
  const bulkInspectorOptions = selectedCommunity
    ? assignment.inspectors_by_community[selectedCommunity] || []
    : []
  const selectableRows = rows.filter(task => {
    const community = assignmentCommunity(task)
    return canBulkAssign
      && !task.inspector
      && task.state !== 'completed'
      && Boolean(community)
      && Boolean(assignment.inspectors_by_community[community]?.length)
  })
  const selectedCount = selectedRows.size

  useEffect(() => {
    const visibleKeys = new Set(rows.map(task => task.row_key))
    setSelectedRows(current => new Set([...current].filter(key => visibleKeys.has(key))))
  }, [rows])

  useEffect(() => {
    setSelectionMode(false)
    setSelectedRows(new Set())
    setBulkInspector(undefined)
  }, [parserType, scope, status, reviewStage, priority, sort, keyword, communities, inspectors, watchCategories])

  const toggleSelected = (rowKey: string, checked: boolean) => {
    const task = rows.find(item => item.row_key === rowKey)
    const community = task ? assignmentCommunity(task) : ''
    if (checked && selectedCommunity && community !== selectedCommunity) {
      message.warning(`本次已锁定为${selectedCommunity}，请分开选择其他社区任务`)
      return
    }
    setSelectedRows(current => {
      const next = new Set(current)
      if (checked) next.add(rowKey)
      else next.delete(rowKey)
      return next
    })
  }

  const selectAllLoaded = () => {
    const communities = Array.from(new Set(selectableRows.map(assignmentCommunity)))
    const targetCommunity = selectedCommunity || (communities.length === 1 ? communities[0] : '')
    if (!targetCommunity) {
      message.info('请先筛选到一个社区，或先勾选一条任务再全选')
      return
    }
    setSelectedRows(current => {
      const next = new Set(current)
      selectableRows
        .filter(task => assignmentCommunity(task) === targetCommunity)
        .forEach(task => next.add(task.row_key))
      return next
    })
  }

  const leaveSelectionMode = () => {
    setSelectionMode(false)
    setSelectedRows(new Set())
    setBulkInspector(undefined)
  }

  const submitBulkAssignment = async () => {
    if (!bulkInspector || !selectedRows.size) return
    setBulkSaving(true)
    try {
      const result = await bulkAssignMobileTasks(parserType, {
        row_keys: [...selectedRows],
        inspector: bulkInspector,
      })
      if (result.updated) message.success(`已分配 ${result.updated} 条任务给 ${result.inspector}`)
      if (result.skipped) message.warning(`有 ${result.skipped} 条任务未处理，请查看原因后刷新`)
      setSelectionMode(false)
      setSelectedRows(new Set())
      setBulkOpen(false)
      setBulkInspector(undefined)
      await load(1)
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '批量分配失败')
    } finally {
      setBulkSaving(false)
    }
  }

  const loadOptions = useCallback(async () => {
    const requestId = ++optionsRequestId.current
    setOptionsLoading(true)
    try {
      const result = await getMobileTaskFilterOptions(parserType, scope, communities)
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
  }, [communities, parserType, scope])

  useEffect(() => { void loadOptions() }, [loadOptions])

  useEffect(() => {
    if (bulkInspector && !bulkInspectorOptions.includes(bulkInspector)) {
      setBulkInspector(undefined)
    }
  }, [bulkInspector, bulkInspectorOptions])

  const load = useCallback(async (targetPage = 1, append = false) => {
    append ? setLoadingMore(true) : setLoading(true)
    setError('')
    try {
      const result = await listMobileTasks({
        parser_type: parserType,
        scope,
        status,
        review_stage: reviewStage,
        communities,
        inspectors,
        watch_categories: watchCategories,
        priority,
        sort,
        keyword: keyword || undefined,
        page: targetPage,
        page_size: 50,
      })
      setRows(current => append ? [...current, ...result.data] : result.data)
      setTotal(result.total)
      setPage(targetPage)
      setFacets(result.facets || EMPTY_FACETS)
      setSourceMessage(result.message || '')
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || reason?.message || '任务列表读取失败')
      if (!append) setRows([])
    } finally {
      setLoading(false)
      setLoadingMore(false)
    }
  }, [communities, inspectors, keyword, parserType, priority, reviewStage, scope, sort, status, watchCategories])

  useEffect(() => { void load() }, [load])

  useEffect(() => {
    const next = new URLSearchParams()
    next.set('type', parserType)
    next.set('scope', scope)
    next.set('status', status)
    if (reviewStage !== 'all') next.set('review_stage', reviewStage)
    communities.forEach(value => next.append('community', value))
    inspectors.forEach(value => next.append('inspector', value))
    watchCategories.forEach(value => next.append('watch_category', String(value)))
    if (priority !== 'all') next.set('priority', priority)
    if (sort !== 'priority') next.set('sort', sort)
    setSearchParams(next, { replace: true })
  }, [communities, inspectors, parserType, priority, reviewStage, scope, setSearchParams, sort, status, watchCategories])

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
    setStatus('pending')
    setReviewStage('all')
    setSearchParams(next)
  }

  const clearFilters = () => {
    setCommunities([])
    setInspectors([])
    setWatchCategories([])
    setPriority('all')
    setSort('priority')
    setStatus('pending')
    setReviewStage('all')
    setKeyword('')
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
    || priority !== 'all'
    || status !== 'pending'
    || reviewStage !== 'all'
    || sort !== 'priority'
    || Boolean(keyword)

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
    <div className="mobile-task-page">
      <section className="app-card mobile-task-filter-card">
        <div className="mobile-task-filter-grid">
          <Select
            size="large"
            value={parserType}
            onChange={value => updateQuery(value, scope)}
            options={MOBILE_TASK_TYPES.map(value => ({ value, label: value }))}
          />
          {adminMode ? (
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
              onPressEnter={() => setKeyword(keywordInput.trim())}
            />
            <Button type="primary" className="min-h-11" onClick={() => setKeyword(keywordInput.trim())}>查询</Button>
          </div>
        </div>

        <div className="mobile-task-priority-grid" aria-label="任务快捷筛选">
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
            <Select
              value={status}
              options={STATUS_OPTIONS}
              onChange={value => setStatus(value as MobileTaskStatus)}
              placeholder="精确任务状态"
            />
            <Select
              value={reviewStage}
              options={[
                { label: '全部复核', value: 'all' },
                { label: '等待研判', value: 'waiting_analysis' },
                { label: '已研判', value: 'analyzed' },
              ]}
              onChange={value => setReviewStage(value as MobileTaskReviewStage)}
              placeholder="复核阶段"
            />
            <Select
              value={priority}
              options={PRIORITY_OPTIONS}
              onChange={value => setPriority(value as MobileTaskPriority)}
              placeholder="优先级"
            />
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
      </section>

      {error && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={() => void load()}>重试</Button>} />}
      {sourceMessage && <Alert type="warning" showIcon message={sourceMessage} />}

      <div className="flex items-center justify-between px-1 text-sm text-[var(--app-text-secondary)]">
        <span>当前筛选共 {total} 条</span>
        {keyword && <button type="button" className="text-[var(--app-primary)]" onClick={() => { setKeyword(''); setKeywordInput('') }}>清除搜索</button>}
      </div>

      {canBulkAssign && (
        <section className="app-card mobile-task-bulk-toolbar">
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type={selectionMode ? 'primary' : 'default'}
              size="small"
              icon={<CheckSquareOutlined />}
              disabled={!selectionMode && !selectableRows.length}
              onClick={() => selectionMode ? leaveSelectionMode() : setSelectionMode(true)}
            >
              {selectionMode ? '退出选择' : '选择'}
            </Button>
            {selectionMode && (
              <>
                <Button size="small" onClick={selectAllLoaded} disabled={!selectableRows.length}>
                  全选未分配任务
                </Button>
                <Button size="small" onClick={() => setSelectedRows(new Set())} disabled={!selectedCount}>
                  清空
                </Button>
                <span className="text-xs text-[var(--app-text-secondary)]">已选 {selectedCount} 条</span>
                <Button
                  type="primary"
                  size="small"
                  disabled={!selectedCount}
                  onClick={() => setBulkOpen(true)}
                >
                  批量分配核查人
                </Button>
              </>
            )}
          </div>
          <div className="mt-1 text-xs text-[var(--app-text-secondary)]">
            {selectionMode
              ? '直接点击卡片进行多选；选中第一条后会锁定同一社区，只处理未分配任务。'
              : '点击“选择”进入多选模式，再直接点击任务卡片进行批量分配。'}
          </div>
        </section>
      )}

      {loading ? (
        <div className="mobile-task-list"><div className="app-card p-4"><Skeleton active paragraph={{ rows: 3 }} /></div><div className="app-card p-4"><Skeleton active paragraph={{ rows: 3 }} /></div></div>
      ) : rows.length === 0 ? (
        <div className="app-card py-8"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有符合条件的任务" /></div>
      ) : (
        <div className="mobile-task-list">
          {rows.map(task => {
            const state = STATE_LABELS[task.state]
            const phoneOptions = mobileTaskPhoneOptions(task.summary.phone)
            const copyPhones = phoneOptions.length > 0
              ? phoneOptions
              : task.summary.phone ? [task.summary.phone] : []
            const primaryPhone = copyPhones[0] || ''
            const extraPhoneCount = Math.max(copyPhones.length - 1, 0)
            const taskCommunity = assignmentCommunity(task)
            const isAssignable = canBulkAssign
              && !task.inspector
              && task.state !== 'completed'
              && Boolean(taskCommunity)
              && Boolean(assignment.inspectors_by_community[taskCommunity]?.length)
            const canSelect = isAssignable
              && (!selectedCommunity || selectedCommunity === taskCommunity)
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
            const openOrSelectTask = () => {
              if (selectionMode) {
                if (!isAssignable) {
                  message.info('该任务当前不能参与批量分配')
                  return
                }
                toggleSelected(task.row_key, !isSelected)
                return
              }
              navigate(`/tasks/${encodeURIComponent(task.parser_type)}/${task.row_key}?scope=${scope}`)
            }
            return (
              <article
                key={task.row_key}
                role="button"
                tabIndex={0}
                aria-pressed={selectionMode ? isSelected : undefined}
                aria-disabled={selectionMode && !canSelect ? true : undefined}
                className={[
                  'mobile-task-item-card',
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
                        {primaryPhone && (
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
                            className="mobile-phone-native-select--header"
                            buttonProps={{
                              type: 'text',
                              className: 'mobile-task-item-card__phone-copy',
                            }}
                            onSelect={phone => void copyValue(phone, '手机号')}
                          />
                        )}
                      </div>
                    </div>
                    <Tag color={state.color} className="mobile-task-item-card__state">{state.text}</Tag>
                  </div>
                  {(task.needs_review
                    || task.review_stage === 'waiting_analysis'
                    || task.review_stage === 'analyzed'
                    || task.conflict
                    || task.source_count > 1
                    || task.pending_sync
                    || Boolean(task.watch_marks?.length)) && (
                    <div className="mobile-task-item-card__flags">
                      {task.needs_review && <Tag color="warning" icon={<ExclamationCircleOutlined />}>需复核</Tag>}
                      {task.review_stage === 'waiting_analysis' && <Tag color="volcano">等待研判</Tag>}
                      {task.review_stage === 'analyzed' && <Tag color="purple">已研判</Tag>}
                      {(task.conflict || task.source_count > 1) && <Tag color="red">来源异常</Tag>}
                      {task.pending_sync && <Tag color="blue">待同步</Tag>}
                      {task.watch_marks?.map(mark => (
                        <Tag key={`${task.row_key}-${mark.category_id}`} color={mark.color}>{mark.name}</Tag>
                      ))}
                    </div>
                  )}
                  {(task.summary.identity_number || primaryAddress) && (
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
                      {primaryAddress && (
                        <div className="mobile-task-item-card__key-row mobile-task-item-card__key-row--address">
                          <dt>{currentAddress ? '现住址' : '地址'}</dt>
                          <dd title={primaryAddress}>{primaryAddress}</dd>
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
                      {task.first_dispatch_at
                        ? `首次下发 ${task.first_dispatch_at}`
                        : task.summary.date || (task.source_count > 1 ? `${task.source_count} 条腾讯来源` : '点击进入处理')}
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
          {rows.length < total && (
            <Button block className="mobile-task-load-more min-h-11" loading={loadingMore} onClick={() => void load(page + 1, true)}>加载更多</Button>
          )}
        </div>
      )}

      <Modal
        open={bulkOpen}
        title="批量分配核查人"
        okText="确认分配"
        cancelText="取消"
        confirmLoading={bulkSaving}
        okButtonProps={{ disabled: !bulkInspector || !selectedCount || !selectedCommunity }}
        onOk={() => void submitBulkAssignment()}
        onCancel={() => { if (!bulkSaving) setBulkOpen(false) }}
      >
        <div className="space-y-3 text-sm">
          {selectedCommunity ? (
            <p>将把当前选中的 {selectedCount} 条{selectedCommunity}任务分配给该社区的一名在岗组员。</p>
          ) : (
            <Alert type="warning" showIcon message="批量分配必须一次只选择同一社区的任务" />
          )}
          <Select
            className="w-full"
            size="large"
            showSearch
            optionFilterProp="label"
            placeholder="请选择核查人"
            value={bulkInspector}
            options={bulkInspectorOptions.map(value => ({ value, label: value }))}
            onChange={setBulkInspector}
          />
          <p className="text-xs text-[var(--app-text-secondary)]">已有核查人的任务、已完成任务、来源冲突任务会被跳过，不会被覆盖。</p>
        </div>
      </Modal>
    </div>
  )
}
