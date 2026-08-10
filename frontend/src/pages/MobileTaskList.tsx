import {
  ExclamationCircleOutlined,
  PhoneOutlined,
  RightOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import { Alert, Button, Checkbox, Empty, Input, Modal, Segmented, Select, Skeleton, Tag, message } from 'antd'
import { useCallback, useEffect, useState } from 'react'
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
import { isFlowTaskAdmin, MOBILE_TASK_TYPES } from '../utils/mobileTaskRouting'
import {
  mobileTaskCanLaunchTelephone,
  mobileTaskPhoneOptions,
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
  const adminMode = isFlowTaskAdmin(
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
  const [selectedRows, setSelectedRows] = useState<Set<string>>(new Set())
  const [bulkOpen, setBulkOpen] = useState(false)
  const [bulkInspector, setBulkInspector] = useState<string | undefined>()
  const [bulkSaving, setBulkSaving] = useState(false)
  const isGroupLeader = user?.member?.position === '组长'

  const selectableRows = rows.filter(task => !task.inspector && task.state !== 'completed')
  const selectedCount = selectedRows.size

  useEffect(() => {
    const visibleKeys = new Set(rows.map(task => task.row_key))
    setSelectedRows(current => new Set([...current].filter(key => visibleKeys.has(key))))
  }, [rows])

  useEffect(() => {
    setSelectedRows(new Set())
    setBulkInspector(undefined)
  }, [parserType, scope, status, reviewStage, priority, sort, keyword, communities, inspectors, watchCategories])

  const toggleSelected = (rowKey: string, checked: boolean) => {
    setSelectedRows(current => {
      const next = new Set(current)
      if (checked) next.add(rowKey)
      else next.delete(rowKey)
      return next
    })
  }

  const selectAllLoaded = () => {
    setSelectedRows(current => {
      const next = new Set(current)
      selectableRows.forEach(task => next.add(task.row_key))
      return next
    })
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
    setOptionsLoading(true)
    try {
      const result = await getMobileTaskFilterOptions(parserType, scope)
      setCommunityOptions(result.communities)
      setInspectorOptions(result.inspectors)
      setWatchCategoryOptions(result.watch_categories || [])
      const communityValues = new Set(result.communities.map(option => option.value))
      const inspectorValues = new Set(result.inspectors.map(option => option.value))
      setCommunities(current => current.filter(value => communityValues.has(value)))
      setInspectors(current => current.filter(value => inspectorValues.has(value)))
      const watchValues = new Set((result.watch_categories || []).map(option => option.value))
      setWatchCategories(current => current.filter(value => watchValues.has(value)))
    } catch {
      setCommunityOptions([])
      setInspectorOptions([])
      setWatchCategoryOptions([])
    } finally {
      setOptionsLoading(false)
    }
  }, [parserType, scope])

  useEffect(() => { void loadOptions() }, [loadOptions])

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
            onChange={values => setCommunities(values)}
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

      {isGroupLeader && (
        <section className="app-card mobile-task-bulk-toolbar">
          <div className="flex flex-wrap items-center gap-2">
            <Button size="small" onClick={selectAllLoaded} disabled={!selectableRows.length}>
              全选当前列表未分配任务
            </Button>
            <Button size="small" onClick={() => setSelectedRows(new Set())} disabled={!selectedCount}>
              清除选择
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
          </div>
          <div className="mt-1 text-xs text-[var(--app-text-secondary)]">
            仅处理当前选择中尚未分配核查人的任务，不会覆盖已有分配；分配对象只能是本社区在岗组员。
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
            const phoneDisplay = phoneOptions.length > 0
              ? phoneOptions.join('、')
              : task.summary.phone
            const canSelect = isGroupLeader && !task.inspector && task.state !== 'completed'
            return (
              <article
                key={task.row_key}
                role="button"
                tabIndex={0}
                className="mobile-task-item-card"
                onClick={() => navigate(`/tasks/${encodeURIComponent(task.parser_type)}/${task.row_key}?scope=${scope}`)}
                onKeyDown={event => { if (event.key === 'Enter') navigate(`/tasks/${encodeURIComponent(task.parser_type)}/${task.row_key}?scope=${scope}`) }}
              >
                <div className="mobile-task-item-card__header">
                  <div className="flex min-w-0 items-start gap-2">
                    {isGroupLeader && (
                      <Checkbox
                        className="mt-1"
                        checked={selectedRows.has(task.row_key)}
                        disabled={!canSelect}
                        onClick={event => event.stopPropagation()}
                        onChange={event => toggleSelected(task.row_key, event.target.checked)}
                      />
                    )}
                    <div className="min-w-0">
                      <div className="mobile-task-item-card__title-row">
                        <h2 title={task.summary.title}>{task.summary.title}</h2>
                        <Tag color={state.color}>{state.text}</Tag>
                      </div>
                      <p className="mobile-task-item-card__assignment">
                        <span>{task.community || '社区未填写'}</span>
                        <span aria-hidden="true">·</span>
                        <span>{task.inspector || '待分配'}</span>
                      </p>
                    </div>
                  </div>
                  <RightOutlined className="mt-1 shrink-0 text-[var(--app-text-muted)]" />
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
                {(task.summary.identity_number || phoneDisplay) && (
                  <dl className="mobile-task-item-card__details">
                    {task.summary.identity_number && <div className="mobile-task-item-card__detail-row mobile-task-item-card__detail-row--primary"><dt>身份证号</dt><dd className="mobile-task-item-card__identity">{task.summary.identity_number}</dd></div>}
                    {phoneDisplay && <div className="mobile-task-item-card__detail-row mobile-task-item-card__detail-row--primary"><dt>手机号</dt><dd className="mobile-task-item-card__phone">{phoneDisplay}</dd></div>}
                  </dl>
                )}
                {task.summary.address && <p className="mobile-task-item-card__address line-clamp-2 text-sm text-[var(--app-text)]">{task.summary.address}</p>}
                {task.summary.source && <Tag className="mobile-task-item-card__source-tag">来源：{task.summary.source}</Tag>}
                {task.review_stage === 'analyzed' && task.summary.analysis && (
                  <div className="mobile-task-analysis">
                    <div className="mobile-task-analysis__label">研判结果</div>
                    <div className="mobile-task-analysis__value">{task.summary.analysis}</div>
                  </div>
                )}
                <div className="mobile-task-item-card__footer flex items-center justify-between gap-3 border-t border-[var(--app-border)]">
                  <div className="min-w-0 text-xs text-[var(--app-text-secondary)]">
                    {task.first_dispatch_at
                      ? `首次下发 ${task.first_dispatch_at}`
                      : task.summary.date || (task.source_count > 1 ? `${task.source_count} 条腾讯来源` : '点击进入处理')}
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
        okButtonProps={{ disabled: !bulkInspector || !selectedCount }}
        onOk={() => void submitBulkAssignment()}
        onCancel={() => { if (!bulkSaving) setBulkOpen(false) }}
      >
        <div className="space-y-3 text-sm">
          <p>将把当前选中的 {selectedCount} 条未分配任务手动分配给一名本社区在岗组员。</p>
          <Select
            className="w-full"
            size="large"
            showSearch
            optionFilterProp="label"
            placeholder="请选择核查人"
            value={bulkInspector}
            options={inspectorOptions.map(option => ({ value: option.value, label: option.label }))}
            onChange={setBulkInspector}
          />
          <p className="text-xs text-[var(--app-text-secondary)]">已有核查人的任务、已完成任务、来源冲突任务会被跳过，不会被覆盖。</p>
        </div>
      </Modal>
    </div>
  )
}
