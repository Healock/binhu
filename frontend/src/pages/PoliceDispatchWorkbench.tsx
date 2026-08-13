import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Collapse,
  Drawer,
  Empty,
  Input,
  Modal,
  Pagination,
  Popconfirm,
  Progress,
  Segmented,
  Select,
  Space,
  Spin,
  Tag,
  message,
} from 'antd'
import {
  CheckOutlined,
  CopyOutlined,
  DeleteOutlined,
  ExclamationCircleOutlined,
  ReloadOutlined,
  SearchOutlined,
  SendOutlined,
} from '@ant-design/icons'
import { useSearchParams } from 'react-router-dom'
import {
  bulkReviewPoliceDispatchTasks,
  deletePoliceDispatchBatch,
  getPoliceDispatchTask,
  getPoliceDispatchWorkbench,
  listPoliceDispatchTasks,
  publishPoliceDispatchBatch,
  resolvePoliceDispatchConflict,
  reviewPoliceDispatchTask,
  updatePoliceDispatchBusinessFields,
  type PoliceCommunityOption,
  type PoliceDispatchBatch,
  type PoliceDispatchTask,
} from '../api/client'
import { useAuth } from '../context/AuthContext'
import useDebouncedValue from '../hooks/useDebouncedValue'
import { ListToolbar } from '../components/ui'
import useMobileViewport from '../hooks/useMobileViewport'
import { mobileTaskSourceTags } from '../utils/mobileTasks'

const actionLabels: Record<string, string> = {
  dispatch: '下发到社区',
  no_registration: '无需登记',
  transfer: '移交',
  duplicate_exclude: '重复排除',
  manual: '待研判',
  '': '待审核',
}

const statusOptions = [
  { label: '待审核', value: 'pending_review' },
  { label: '待发布', value: 'pending_publish' },
  { label: '可重试', value: 'retryable' },
  { label: '待对账', value: 'needs_reconciliation' },
  { label: '内容冲突', value: 'conflict' },
  { label: '已完成', value: 'completed' },
  { label: '全部', value: 'all' },
]

const categoryOptions = [
  { label: '全部分类', value: 'all' },
  { label: '社区下发', value: 'dispatch' },
  { label: '无需登记', value: 'no_registration' },
  { label: '移交', value: 'transfer' },
  { label: '模糊分配', value: 'balanced' },
  { label: '重复', value: 'duplicate' },
  { label: '待研判', value: 'manual' },
]

function CardCopyValue({ value, label }: { value: string; label: string }) {
  return (
    <button
      type="button"
      className="mobile-task-copy-value"
      title={`点击复制${label}`}
      aria-label={`复制${label}`}
      onClick={async event => {
        event.stopPropagation()
        try {
          await navigator.clipboard.writeText(value)
          message.success(`${label}已复制`)
        } catch {
          message.error(`${label}复制失败，请长按或选中文字复制`)
        }
      }}
      onKeyDown={event => event.stopPropagation()}
    >
      <span className="mobile-task-copy-value__text">{value}</span>
      <CopyOutlined aria-hidden="true" />
    </button>
  )
}

function TaskCard({ item, onOpen }: { item: PoliceDispatchTask; onOpen: () => void }) {
  const taskStatus = item.publish_status === 'needs_reconciliation'
    ? { color: 'warning', text: '等待同步对账' }
    : item.publish_status === 'conflict'
      ? { color: 'error', text: '内容冲突' }
      : item.publish_status === 'retryable'
        ? { color: 'orange', text: '可安全重试' }
        : item.task_status === 'pending_review' && item.suggested_action === 'manual'
          ? { color: 'warning', text: '待研判' }
          : item.task_status === 'pending_review'
          ? { color: 'orange', text: '待审核' }
          : item.task_status === 'completed'
            ? { color: 'success', text: '已完成' }
            : { color: 'processing', text: '待发布' }
  const communityName = item.final_community_name || item.suggested_community_name || '社区待确定'
  const actionName = actionLabels[item.final_action || item.suggested_action] || '待处理'
  const suggestedActionName = actionLabels[item.suggested_action] || '等待人工判断'
  const sourceTags = mobileTaskSourceTags(item.source_name)
  return (
    <article
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={event => {
        if (event.target === event.currentTarget && event.key === 'Enter') onOpen()
      }}
      className="mobile-task-item-card police-dispatch-task-card"
    >
      <div className="mobile-task-item-card__body">
        <div className="mobile-task-item-card__header">
          <div className="mobile-task-item-card__header-main">
            <div className="mobile-task-item-card__title-row">
              <h2 title={item.person_name || '姓名缺失'}>{item.person_name || '姓名缺失'}</h2>
            </div>
          </div>
          <Tag color={taskStatus.color} className="mobile-task-item-card__state">{taskStatus.text}</Tag>
        </div>

        {(item.duplicate_group_key || item.allocation_mode === 'balanced' || item.suggested_action === 'manual') && (
          <div className="mobile-task-item-card__flags">
            {item.duplicate_group_key && (
              <Tag color="orange" icon={<ExclamationCircleOutlined />}>
                {item.duplicate_kind === 'exact' ? '同批完全重复' : '同身份证信息有差异'}
              </Tag>
            )}
            {item.allocation_mode === 'balanced' && <Tag color="blue">模糊地址 · 平均分配</Tag>}
            {item.suggested_action === 'manual' && <Tag color="red">需人工研判</Tag>}
          </div>
        )}

        <dl className="mobile-task-item-card__key-info">
          {item.identity_number && (
            <div className="mobile-task-item-card__key-row mobile-task-item-card__key-row--identity">
              <dt>身份证号</dt>
              <dd className="mobile-task-item-card__identity">
                <CardCopyValue value={item.identity_number} label="身份证号" />
              </dd>
            </div>
          )}
          {item.phone && (
            <div className="mobile-task-item-card__key-row mobile-task-item-card__key-row--phone">
              <dt>手机号</dt>
              <dd><CardCopyValue value={item.phone} label="手机号" /></dd>
            </div>
          )}
          {item.original_address && (
            <div className="mobile-task-item-card__key-row mobile-task-item-card__key-row--address">
              <dt>地址</dt>
              <dd>
                <CardCopyValue value={item.original_address} label="地址" />
              </dd>
            </div>
          )}
        </dl>

        <div className="mobile-task-analysis police-dispatch-task-card__suggestion">
          <div className="mobile-task-analysis__label">平台建议</div>
          <div className="mobile-task-analysis__value">
            {suggestedActionName}{item.suggested_community_name ? ` · ${item.suggested_community_name}` : ''}
          </div>
          <div className="police-dispatch-task-card__suggestion-reason">
            {item.suggestion_reason || '等待人工判断'}
          </div>
        </div>

        {sourceTags.length > 0 && (
          <div className="mobile-task-source-cloud mobile-task-source-cloud--card">
            <div>
              {sourceTags.map(tag => (
                <Tag key={`${item.id}-${tag}`} className="mobile-task-source-cloud__tag">{tag}</Tag>
              ))}
            </div>
          </div>
        )}

        {item.publish_error && (
          <div className="police-dispatch-task-card__error">{item.publish_error}</div>
        )}
      </div>
      <div className="mobile-task-item-card__footer">
        <div className="mobile-task-item-card__footer-meta">
          <div className="mobile-task-item-card__ownership">
            <span title={communityName}>{communityName}</span>
            <span aria-hidden="true">·</span>
            <span title={actionName}>{actionName}</span>
          </div>
          <div className="mobile-task-item-card__date">
            {item.created_time || `Excel 第 ${item.source_row} 行`}
          </div>
        </div>
      </div>
    </article>
  )
}

export default function PoliceDispatchWorkbench({ mode = 'all' }: { mode?: 'all' | 'analysis' }) {
  const { user } = useAuth()
  const mobile = useMobileViewport()
  const [searchParams, setSearchParams] = useSearchParams()
  const [batches, setBatches] = useState<PoliceDispatchBatch[]>([])
  const [communities, setCommunities] = useState<PoliceCommunityOption[]>([])
  const [batchId, setBatchId] = useState<number | null>(null)
  const [tasks, setTasks] = useState<PoliceDispatchTask[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const analysisOnly = mode === 'analysis'
  const requestedStatus = analysisOnly ? 'pending_review' : (searchParams.get('status') || 'pending_review')
  const requestedCategory = analysisOnly ? 'manual' : (searchParams.get('category') || 'all')
  const [status, setStatus] = useState(
    statusOptions.some(option => option.value === requestedStatus) ? requestedStatus : 'pending_review',
  )
  const [category, setCategory] = useState(
    categoryOptions.some(option => option.value === requestedCategory) ? requestedCategory : 'all',
  )
  const [keyword, setKeyword] = useState('')
  const [keywordFlush, setKeywordFlush] = useState(0)
  const appliedKeyword = useDebouncedValue(keyword.trim(), 350, keywordFlush)
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [selected, setSelected] = useState<PoliceDispatchTask | null>(null)
  const [duplicates, setDuplicates] = useState<PoliceDispatchTask[]>([])
  const [duplicateDifferences, setDuplicateDifferences] = useState<Array<{
    task_id: number
    source_row: number
    fields: Array<{ field: string; value: string }>
  }>>([])
  const [fieldDraft, setFieldDraft] = useState<Record<string, string>>({})
  const [finalAction, setFinalAction] = useState<Exclude<PoliceDispatchTask['final_action'], ''>>('dispatch')
  const [finalCommunityId, setFinalCommunityId] = useState<number | null>(null)
  const [reviewNote, setReviewNote] = useState('')
  const [saving, setSaving] = useState(false)
  const [fieldSaving, setFieldSaving] = useState(false)
  const [deletingBatch, setDeletingBatch] = useState(false)
  const [publishingBatch, setPublishingBatch] = useState(false)
  const [error, setError] = useState('')
  const taskRequestId = useRef(0)

  const isSuperAdmin = Boolean(
    user?.permission_groups?.some(group => group.code === 'super_admin')
    || user?.permission_group?.code === 'super_admin'
    || (!user?.permission_groups?.length && user?.role === 'super_admin'),
  )

  const activeBatch = useMemo(
    () => batches.find(item => item.id === batchId) || null,
    [batches, batchId],
  )
  const publishableCount = activeBatch
    ? activeBatch.import_mode === 'clean' && activeBatch.counts.pending_review > 0
      ? activeBatch.counts.partial_publishable
      : activeBatch.counts.publishable
    : 0

  const loadHome = async () => {
    try {
      const result = await getPoliceDispatchWorkbench()
      setBatches(result.batches)
      setCommunities(result.communities)
      const requested = Number(searchParams.get('batch'))
      const nextId = result.batches.some(item => item.id === requested)
        ? requested
        : result.active_batch?.id || null
      setBatchId(nextId)
      if (nextId) {
        const next = new URLSearchParams(searchParams)
        next.set('batch', String(nextId))
        next.set('status', analysisOnly ? 'pending_review' : status)
        next.set('category', analysisOnly ? 'manual' : category)
        setSearchParams(next, { replace: true })
      }
      setError('')
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '任务工作台读取失败')
      setLoading(false)
    }
  }

  const loadTasks = async (targetPage = page) => {
    if (!batchId) {
      setTasks([])
      setTotal(0)
      setLoading(false)
      return
    }
    const requestId = ++taskRequestId.current
    setLoading(true)
    try {
      const result = await listPoliceDispatchTasks({
        batch_id: batchId,
        status: analysisOnly ? 'pending_review' : status,
        category: analysisOnly ? 'manual' : category,
        keyword: appliedKeyword,
        page: targetPage,
        page_size: 20,
      })
      if (requestId !== taskRequestId.current) return
      setTasks(result.data)
      setTotal(result.total)
      setPage(targetPage)
      setError('')
    } catch (reason: any) {
      if (requestId === taskRequestId.current) setError(reason?.response?.data?.detail || '任务列表读取失败')
    } finally {
      if (requestId === taskRequestId.current) setLoading(false)
    }
  }

  useEffect(() => { void loadHome() }, [])
  useEffect(() => { if (batchId) void loadTasks(1) }, [batchId, status, category, appliedKeyword])

  useEffect(() => {
    if (!batchId) return
    const next = new URLSearchParams(searchParams)
    next.set('batch', String(batchId))
    next.set('status', analysisOnly ? 'pending_review' : status)
    next.set('category', analysisOnly ? 'manual' : category)
    setSearchParams(next, { replace: true })
  }, [analysisOnly, batchId, category, setSearchParams, status])

  const changeBatch = (value: number) => {
    setBatchId(value)
    const next = new URLSearchParams(searchParams)
    next.set('batch', String(value))
    next.set('status', analysisOnly ? 'pending_review' : status)
    next.set('category', analysisOnly ? 'manual' : category)
    setSearchParams(next, { replace: true })
    setPage(1)
  }

  const deleteActiveBatch = () => {
    if (!activeBatch || !isSuperAdmin) return
    Modal.confirm({
      title: `删除批次 #${activeBatch.id}？`,
      content: (
        <div className="space-y-2 text-sm">
          <p>将删除该批次及其中 {activeBatch.total_count} 条本地审核任务，删除后不可恢复。</p>
          <p className="text-slate-500">已经开始发布或存在腾讯来源关联的批次不能删除。</p>
        </div>
      ),
      okText: '删除批次',
      cancelText: '取消',
      okButtonProps: { danger: true, loading: deletingBatch },
      async onOk() {
        setDeletingBatch(true)
        try {
          const result = await deletePoliceDispatchBatch(activeBatch.id)
          message.success(`${result.message}，已移除 ${result.deleted_task_count} 条任务`)
          setSelected(null)
          setBatchId(null)
          setTasks([])
          setTotal(0)
          setSearchParams({}, { replace: true })
          await loadHome()
        } catch (reason: any) {
          message.error(reason?.response?.data?.detail || '批次删除失败')
          throw reason
        } finally {
          setDeletingBatch(false)
        }
      },
    })
  }

  const publishActiveBatch = async () => {
    if (!activeBatch || publishingBatch) return
    setPublishingBatch(true)
    try {
      const result = await publishPoliceDispatchBatch(activeBatch.id)
      message[result.failed_count ? 'warning' : 'success'](result.message)
      await loadHome()
      await loadTasks(1)
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '整批发布失败')
    } finally {
      setPublishingBatch(false)
    }
  }

  const openTask = async (item: PoliceDispatchTask) => {
    setDetailLoading(true)
    setSelected(item)
    try {
      const result = await getPoliceDispatchTask(item.id)
      setSelected(result.task)
      setDuplicates(result.duplicates)
      setDuplicateDifferences(result.duplicate_differences || [])
      setFieldDraft(result.task.raw_values || {})
      setFinalAction(
        result.task.final_action
        || (result.task.suggested_action === 'manual' ? 'dispatch' : result.task.suggested_action),
      )
      setFinalCommunityId(result.task.final_community_id || result.task.suggested_community_id)
      setReviewNote(result.task.review_note || '')
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '任务详情读取失败')
      setSelected(null)
    } finally {
      setDetailLoading(false)
    }
  }

  const saveBusinessFields = async () => {
    if (!selected) return
    const changed = Object.fromEntries(
      Object.entries(fieldDraft).filter(([field, value]) => (
        value !== String(selected.raw_values?.[field] || '')
      )),
    )
    if (!Object.keys(changed).length) {
      message.info('业务字段没有变化')
      return
    }
    setFieldSaving(true)
    try {
      await updatePoliceDispatchBusinessFields(selected.id, {
        expected_version: selected.version,
        fields: changed,
      })
      message.success('业务字段已保存，建议和重复关系已重新计算')
      const refreshed = await getPoliceDispatchTask(selected.id)
      setSelected(refreshed.task)
      setDuplicates(refreshed.duplicates)
      setDuplicateDifferences(refreshed.duplicate_differences || [])
      setFieldDraft(refreshed.task.raw_values || {})
      await Promise.all([loadHome(), loadTasks(page)])
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '业务字段保存失败')
    } finally {
      setFieldSaving(false)
    }
  }

  const resolveConflict = async (strategy: 'adopt_tencent' | 'overwrite_tencent') => {
    if (!selected?.linked_row_hash) return
    const execute = async (confirmation = '') => {
      setSaving(true)
      try {
        const result = await resolvePoliceDispatchConflict(selected.id, {
          expected_version: selected.version,
          expected_row_hash: selected.linked_row_hash,
          strategy,
          confirmation,
        })
        message.success(result.message)
        setSelected(null)
        await Promise.all([loadHome(), loadTasks(page)])
      } catch (reason: any) {
        message.error(reason?.response?.data?.detail || '冲突处理失败')
      } finally {
        setSaving(false)
      }
    }
    if (strategy === 'adopt_tencent') {
      await execute()
      return
    }
    Modal.confirm({
      title: '用平台内容覆盖腾讯现有行？',
      content: '系统会重新校验腾讯行版本，并更新现有行，不会新增重复记录。',
      okText: '确认覆盖',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => execute('覆盖腾讯内容'),
    })
  }

  const saveReview = async () => {
    if (!selected) return
    if (finalAction === 'dispatch' && !finalCommunityId) {
      message.warning('下发任务必须选择社区')
      return
    }
    setSaving(true)
    try {
      await reviewPoliceDispatchTask(selected.id, {
        expected_version: selected.version,
        final_action: finalAction,
        final_community_id: finalAction === 'dispatch' ? finalCommunityId : null,
        review_note: reviewNote,
      })
      message.success('审核结果已保存')
      setSelected(null)
      await Promise.all([loadHome(), loadTasks(page)])
    } catch (reason: any) {
      const detail = reason?.response?.data?.detail || '保存失败'
      message.error(detail)
      if (reason?.response?.status === 409) {
        const refreshed = await getPoliceDispatchTask(selected.id).catch(() => null)
        if (refreshed) setSelected(refreshed.task)
      }
    } finally {
      setSaving(false)
    }
  }

  const acceptCurrentFilter = async () => {
    if (!batchId) return
    if (total > 2000) {
      message.warning('当前筛选超过 2000 条，请缩小筛选范围后再批量确认')
      return
    }
    setSaving(true)
    try {
      const filtered: PoliceDispatchTask[] = []
      let targetPage = 1
      let expectedTotal = total
      do {
        const result = await listPoliceDispatchTasks({
          batch_id: batchId,
          status,
          category,
          keyword: appliedKeyword,
          page: targetPage,
          page_size: 500,
        })
        filtered.push(...result.data)
        expectedTotal = result.total
        targetPage += 1
      } while (filtered.length < expectedTotal)
      const pending = filtered.filter(item => item.task_status === 'pending_review')
      if (!pending.length) {
        message.info('当前筛选结果中没有待审核任务')
        return
      }
      await bulkReviewPoliceDispatchTasks({
        tasks: pending.map(item => ({ id: item.id, version: item.version })),
        mode: 'accept_suggestion',
      })
      message.success(`已确认当前筛选结果中的 ${pending.length} 条建议`)
      await Promise.all([loadHome(), loadTasks(page)])
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '批量确认失败')
      await loadTasks(page)
    } finally {
      setSaving(false)
    }
  }

  const communityOptions = communities.filter(item => item.enabled).map(item => ({
    value: item.id,
    label: item.name,
  }))
  const keyBusinessHeaders = selected ? [
    selected.field_roles.name,
    selected.field_roles.identity,
    selected.field_roles.phone,
    selected.field_roles.address,
  ].filter((value): value is string => Boolean(value)) : []
  const otherBusinessHeaders = selected
    ? Object.keys(selected.raw_values || {}).filter(field => !keyBusinessHeaders.includes(field))
    : []

  return (
    <div className="police-dispatch-workbench mx-auto max-w-7xl space-y-4 pb-4">
      <section className="app-card overflow-hidden border-0 bg-gradient-to-br from-blue-700 to-indigo-700 p-5 text-white shadow-lg">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="text-xs font-medium text-blue-100">内勤业务 · 共享队列</div>
            <h1 className="mt-1 text-xl font-semibold">{analysisOnly ? '下发数据复核' : '下发任务处理'}</h1>
          </div>
          <Button ghost icon={<ReloadOutlined />} onClick={() => Promise.all([loadHome(), loadTasks(page)])}>刷新</Button>
        </div>
        <div className="mt-4 flex items-center gap-2">
          <Select
            className="min-w-0 flex-1"
            value={batchId}
            placeholder="暂无批次"
            onChange={changeBatch}
            options={batches.map(item => ({
              value: item.id,
              label: `#${item.id} · ${item.file_name} · ${item.status === 'completed' ? '已完成' : '处理中'}`,
            }))}
          />
          {isSuperAdmin && activeBatch && (
            <Button
              danger
              icon={<DeleteOutlined />}
              loading={deletingBatch}
              className="shrink-0"
              onClick={deleteActiveBatch}
            >
              <span className="hidden sm:inline">删除批次</span>
            </Button>
          )}
        </div>
        {activeBatch && (
          <>
            <Progress
              className="mt-4"
              strokeColor="#fff"
              railColor="rgba(255,255,255,.24)"
              percent={activeBatch.total_count ? Math.round(activeBatch.reviewed_count / activeBatch.total_count * 100) : 0}
              format={() => <span className="text-white">{activeBatch.reviewed_count}/{activeBatch.total_count}</span>}
            />
            <div className="mt-3 grid grid-cols-4 gap-2 text-center">
              {[
                ['待审核', activeBatch.counts.pending_review],
                ['待发布', activeBatch.counts.pending_publish],
                ['重复', activeBatch.counts.duplicate],
                ['待研判', activeBatch.counts.abnormal],
              ].map(([label, value]) => (
                <div key={String(label)} className="rounded-xl bg-white/10 px-2 py-2.5">
                  <div className="text-lg font-semibold">{value}</div>
                  <div className="text-[11px] text-blue-100">{label}</div>
                </div>
              ))}
            </div>
          </>
        )}
      </section>

      {error && <Alert type="error" showIcon message={error} />}

      <section className="app-card p-3 sm:p-4">
        {!analysisOnly && (
          <div className="overflow-x-auto pb-1">
            <Segmented
              block
              className="min-w-[680px] md:min-w-0"
              value={status}
              options={statusOptions}
              onChange={value => setStatus(String(value))}
            />
          </div>
        )}
        {analysisOnly && (
          <Alert
            type="info"
            showIcon
            message="这里处理下发文件中无法直接确定去向的数据；复核结果仍保存在原下发批次。"
          />
        )}
        <ListToolbar
          className="mt-3"
          filters={<>
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="姓名、身份证号、手机号、地址"
            value={keyword}
            onChange={event => setKeyword(event.target.value)}
            onPressEnter={() => setKeywordFlush(current => current + 1)}
          />
          {!analysisOnly && <Select value={category} options={categoryOptions} onChange={setCategory} />}
          </>}
          meta={<span>当前筛选 {total} 条</span>}
          actions={<>
            {!analysisOnly && activeBatch && publishableCount > 0 && (
              <Popconfirm
                title={`整批发布 ${publishableCount} 条已审核任务？`}
                description={activeBatch.counts.pending_review > 0
                  ? `其余 ${activeBatch.counts.pending_review} 条待复核记录不会发布，仍保留在当前批次中。`
                  : '发布后将写入腾讯全链条表；异常、冲突和待对账记录不会重复写入。'}
                okText="确认整批发布"
                cancelText="取消"
                onConfirm={publishActiveBatch}
              >
                <Button
                  type="primary"
                  icon={<SendOutlined />}
                  loading={publishingBatch}
                >
                  整批发布（{publishableCount}）
                </Button>
              </Popconfirm>
            )}
            {!analysisOnly && (
              <Popconfirm
                title="确认当前筛选结果的全部建议？"
                description="最多处理 2000 条；含人工判断的任务会阻止整批操作，请先逐条处理。"
                onConfirm={acceptCurrentFilter}
              >
                <Button
                  icon={<CheckOutlined />}
                  disabled={total === 0 || status === 'pending_publish' || status === 'completed'}
                  loading={saving}
                >
                  批量确认
                </Button>
              </Popconfirm>
            )}
          </>}
        />
      </section>

      <Spin spinning={loading}>
        <div className="mobile-task-list">
          {tasks.map(item => <TaskCard key={item.id} item={item} onOpen={() => openTask(item)} />)}
          {!loading && !tasks.length && <div className="app-card mobile-task-list__empty py-12"><Empty description="当前筛选没有任务" /></div>}
        </div>
      </Spin>

      {total > 20 && (
        <div className="app-card flex justify-center p-3">
          <Pagination simple current={page} pageSize={20} total={total} onChange={loadTasks} />
        </div>
      )}

      <Drawer
        open={Boolean(selected)}
        title={selected ? `${selected.person_name || '待核查对象'} · 第 ${selected.source_row} 行` : '任务审核'}
        placement={mobile ? 'bottom' : 'right'}
        height={mobile ? 'min(88dvh, 820px)' : undefined}
        width={mobile ? undefined : 'min(720px, 88vw)'}
        onClose={() => setSelected(null)}
        destroyOnHidden
        extra={selected && <Tag>v{selected.version}</Tag>}
        footer={(
          selected?.task_status === 'pending_publish'
            && ['pending', 'retryable'].includes(selected.publish_status)
            ? (
                <div className="text-center text-sm text-[var(--app-text-secondary)]">
                  该条已经审核，无需逐条保存；请关闭详情后使用页面上方的“整批发布”。
                </div>
              )
            : (
                <Button
                  block
                  type="primary"
                  size="large"
                  loading={saving}
                  disabled={Boolean(selected && ['success', 'publishing', 'needs_reconciliation', 'conflict'].includes(selected.publish_status))}
                  onClick={saveReview}
                >
                  {selected?.publish_status === 'conflict' ? '请先处理内容冲突' : '保存审核结果'}
                </Button>
              )
        )}
      >
        <Spin spinning={detailLoading}>
          {selected && (
            <div className="space-y-4 pb-4">
              <section className="app-surface-muted rounded-2xl p-4">
                {[
                  ['来源', selected.source_name],
                  ['身份证号', selected.identity_number],
                  ['手机号', selected.phone],
                  ['创建时间', selected.created_time],
                  ['原地址', selected.original_address],
                  ['原移交信息', selected.transfer_note],
                ].map(([label, value]) => value ? (
                  <div key={label} className="mb-3 last:mb-0">
                    <div className="text-xs text-slate-400">{label}</div>
                    <div className="mt-1 flex items-start gap-1 break-all text-sm text-slate-800">
                      <span className="flex-1">{value}</span>
                      {(label === '身份证号' || label === '手机号' || label === '原地址') && <CopyButton value={value} />}
                    </div>
                  </div>
                ) : null)}
              </section>

              <section className="rounded-2xl border border-slate-200 p-4">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <div className="font-medium text-slate-900">导入业务字段</div>
                    <div className="mt-1 text-xs text-slate-500">修改后会重新计算地址建议、社区分配和重复关系，受影响的旧审核会被清除。</div>
                  </div>
                  <Button
                    loading={fieldSaving}
                    disabled={['success', 'publishing', 'needs_reconciliation', 'conflict'].includes(selected.publish_status)}
                    onClick={saveBusinessFields}
                  >
                    保存字段
                  </Button>
                </div>
                <div className="mt-4 space-y-3">
                  {keyBusinessHeaders.map(field => (
                    <label key={field} className="block">
                      <span className="mb-1.5 block text-sm font-medium text-slate-700">{field}</span>
                      {field === selected.field_roles.address ? (
                        <Input.TextArea
                          rows={3}
                          value={fieldDraft[field] || ''}
                          onChange={event => setFieldDraft(current => ({ ...current, [field]: event.target.value }))}
                        />
                      ) : (
                        <Input
                          value={fieldDraft[field] || ''}
                          onChange={event => setFieldDraft(current => ({ ...current, [field]: event.target.value }))}
                        />
                      )}
                    </label>
                  ))}
                </div>
                {otherBusinessHeaders.length > 0 && (
                  <Collapse
                    ghost
                    className="mt-3"
                    items={[{
                      key: 'more-fields',
                      label: `更多字段（${otherBusinessHeaders.length}）`,
                      children: (
                        <div className="space-y-3">
                          {otherBusinessHeaders.map(field => (
                            <label key={field} className="block">
                              <span className="mb-1 block text-xs text-slate-500">{field}</span>
                              <Input.TextArea
                                autoSize={{ minRows: 1, maxRows: 4 }}
                                value={fieldDraft[field] || ''}
                                onChange={event => setFieldDraft(current => ({ ...current, [field]: event.target.value }))}
                              />
                            </label>
                          ))}
                        </div>
                      ),
                    }]}
                  />
                )}
              </section>

              <Alert
                type={selected.suggested_action === 'manual' ? 'warning' : 'info'}
                showIcon
                message={`平台建议：${actionLabels[selected.suggested_action]}${selected.suggested_community_name ? ` · ${selected.suggested_community_name}` : ''}`}
                description={selected.suggestion_reason}
              />

              {duplicates.length > 1 && (
                <section className="rounded-2xl border border-orange-200 bg-orange-50 p-4">
                  <div className="font-medium text-orange-900">同一身份证号共 {duplicates.length} 条</div>
                  <div className="mt-1 text-xs text-orange-700">下面只展示真正不同的字段；请确认保留一条，其余选择“重复排除”。</div>
                  <div className="mt-3 space-y-2">
                    {duplicateDifferences.map(item => (
                      <button
                        type="button"
                        key={item.task_id}
                        className={`w-full rounded-xl border p-3 text-left text-xs ${item.task_id === selected.id ? 'border-orange-500 bg-white' : 'border-orange-200 bg-orange-50/60'}`}
                        onClick={() => {
                          const target = duplicates.find(row => row.id === item.task_id)
                          if (target && target.id !== selected.id) void openTask(target)
                        }}
                      >
                        <div className="font-medium">Excel 第 {item.source_row} 行</div>
                        <div className="mt-2 space-y-1.5">
                          {item.fields.map(field => (
                            <div key={field.field} className="grid grid-cols-[88px_minmax(0,1fr)] gap-2">
                              <span className="text-slate-400">{field.field}</span>
                              <span className="break-all text-slate-700">{field.value || '（空）'}</span>
                            </div>
                          ))}
                        </div>
                      </button>
                    ))}
                  </div>
                </section>
              )}

              {selected.publish_status === 'conflict' && (
                <section className="rounded-2xl border border-red-200 bg-red-50 p-4">
                  <div className="font-medium text-red-900">腾讯内容与平台待发布内容不同</div>
                  <div className="mt-3 space-y-2">
                    {selected.conflict_diff.map(item => (
                      <div key={item.field} className="rounded-xl bg-white p-3 text-xs">
                        <div className="font-medium text-slate-700">{item.field}</div>
                        <div className="mt-2 grid gap-2 sm:grid-cols-2">
                          <div><span className="text-slate-400">平台：</span><span className="break-all">{item.platform || '（空）'}</span></div>
                          <div><span className="text-slate-400">腾讯：</span><span className="break-all">{item.tencent || '（空）'}</span></div>
                        </div>
                      </div>
                    ))}
                  </div>
                  <div className="mt-4 grid grid-cols-2 gap-2">
                    <Button onClick={() => void resolveConflict('adopt_tencent')}>采用腾讯内容</Button>
                    <Button danger onClick={() => void resolveConflict('overwrite_tencent')}>采用平台内容</Button>
                  </div>
                </section>
              )}

              <section className="space-y-3">
                <div>
                  <div className="mb-1.5 text-sm font-medium text-slate-700">最终动作</div>
                  <Select
                    size="large"
                    className="w-full"
                    value={finalAction}
                    onChange={setFinalAction}
                    options={[
                      { value: 'dispatch', label: '下发到社区' },
                      { value: 'no_registration', label: '无需登记' },
                      { value: 'transfer', label: '移交' },
                      ...(selected.duplicate_group_key
                        ? [{ value: 'duplicate_exclude' as const, label: '重复排除' }]
                        : []),
                    ]}
                  />
                </div>
                {finalAction === 'dispatch' && (
                  <div>
                    <div className="mb-1.5 text-sm font-medium text-slate-700">下发社区</div>
                    <Select
                      size="large"
                      showSearch
                      optionFilterProp="label"
                      className="w-full"
                      value={finalCommunityId}
                      onChange={setFinalCommunityId}
                      options={communityOptions}
                    />
                  </div>
                )}
                <div>
                  <div className="mb-1.5 text-sm font-medium text-slate-700">处理说明（可选）</div>
                  <Input.TextArea
                    rows={3}
                    maxLength={1000}
                    showCount
                    value={reviewNote}
                    onChange={event => setReviewNote(event.target.value)}
                  />
                </div>
              </section>
            </div>
          )}
        </Spin>
      </Drawer>
    </div>
  )
}
