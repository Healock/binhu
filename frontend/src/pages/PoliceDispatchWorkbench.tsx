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
  Select,
  Space,
  Spin,
  Tag,
  Tooltip,
  message,
} from 'antd'
import {
  CheckOutlined,
  CheckSquareOutlined,
  CopyOutlined,
  DeleteOutlined,
  ExclamationCircleOutlined,
  FileSearchOutlined,
  ReloadOutlined,
  SearchOutlined,
  SendOutlined,
} from '@ant-design/icons'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  bulkReviewPoliceDispatchTasks,
  deletePoliceDispatchBatch,
  getLatestPoliceDispatchPublishRun,
  getPoliceDispatchPublishRun,
  getPoliceDispatchTask,
  getPoliceDispatchPublishableSelection,
  getPoliceDispatchWorkbench,
  listPoliceDispatchTasks,
  publishSelectedPoliceDispatchTasks,
  resolvePoliceDispatchDuplicateGroup,
  adoptExistingPoliceDispatchContent,
  reviewPoliceDispatchTask,
  updatePoliceDispatchBusinessFields,
  type PoliceCommunityOption,
  type PoliceDispatchBatch,
  type PoliceDispatchPublishRun,
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

const publishStatusOptions = [
  { label: '未发布', value: 'pending_publish' },
  { label: '可重试', value: 'retryable' },
  { label: '待对账', value: 'needs_reconciliation' },
  { label: '内容冲突', value: 'conflict' },
] as const

const statusOptions = [
  { label: '待审核', value: 'pending_review' },
  ...publishStatusOptions,
  { label: '已完成', value: 'completed' },
  { label: '全部', value: 'all' },
]

const reconciliationHint = '本地发布结果仍需核对，系统会继续确认成功、内容冲突或是否可以安全重试。'
const publishStatusValues = publishStatusOptions.map(option => option.value)

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

function CopyIconButton({ value, label }: { value: string; label: string }) {
  return (
    <Button
      type="text"
      size="small"
      icon={<CopyOutlined />}
      title={`复制${label}`}
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
    />
  )
}

function TaskCard({
  item,
  onOpen,
  selectionMode = false,
  selected = false,
  selectable = false,
}: {
  item: PoliceDispatchTask
  onOpen: () => void
  selectionMode?: boolean
  selected?: boolean
  selectable?: boolean
}) {
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
  const standard = item.standard_values || {}
  const displayName = item.person_name || standard['姓名'] || standard['接警编号'] || `Excel 第 ${item.source_row} 行`
  const identity = item.identity_number || standard['身份证号'] || standard['身份证号码'] || ''
  const phone = item.phone || standard['电话号码'] || standard['手机号码'] || standard['联系号码'] || ''
  const address = item.original_address
    || standard['地址'] || standard['地址1'] || standard['疑似现住址'] || standard['高频抓拍小区'] || standard['简要警情及处理结果'] || ''
  const businessLabel = item.police_subtype === 'internal'
    ? '涉警 · 所内涉警'
    : item.police_subtype === 'suzhou'
      ? '涉警 · 苏州涉警'
      : item.police_subtype === 'traffic'
        ? '涉警 · 交通涉警'
        : item.target_parser || item.source_name
  return (
    <article
      role="button"
      tabIndex={0}
      aria-pressed={selectionMode ? selected : undefined}
      aria-disabled={selectionMode && !selectable ? true : undefined}
      onClick={onOpen}
      onKeyDown={event => {
        if (
          event.target === event.currentTarget
          && (event.key === 'Enter' || (selectionMode && event.key === ' '))
        ) {
          event.preventDefault()
          onOpen()
        }
      }}
      className={[
        'mobile-task-item-card police-dispatch-task-card',
        selectionMode ? 'is-selection-mode' : '',
        selected ? 'is-selected' : '',
        selectionMode && !selectable ? 'is-selection-disabled' : '',
      ].filter(Boolean).join(' ')}
    >
      <div className="mobile-task-item-card__body">
        <div className="mobile-task-item-card__header">
          <div className="mobile-task-item-card__header-main">
            <div className="mobile-task-item-card__title-row">
              <h2 title={displayName}>{displayName}</h2>
              <Tag color="blue">{businessLabel}</Tag>
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
          {identity && (
            <div className="mobile-task-item-card__key-row mobile-task-item-card__key-row--identity">
              <dt>身份证号</dt>
              <dd className="mobile-task-item-card__identity">
                <CardCopyValue value={identity} label="身份证号" />
              </dd>
            </div>
          )}
          {phone && (
            <div className="mobile-task-item-card__key-row mobile-task-item-card__key-row--phone">
              <dt>手机号</dt>
              <dd><CardCopyValue value={phone} label="手机号" /></dd>
            </div>
          )}
          {address && (
            <div className="mobile-task-item-card__key-row mobile-task-item-card__key-row--address">
              <dt>地址</dt>
              <dd>
                <CardCopyValue value={address} label="地址" />
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
            {item.created_time || standard['下发日期'] || standard['日期'] || `Excel 第 ${item.source_row} 行`}
          </div>
        </div>
      </div>
    </article>
  )
}

export default function PoliceDispatchWorkbench({
  mode = 'all',
  onAnalysisCountChange,
  manageUrl = true,
}: {
  mode?: 'all' | 'analysis'
  onAnalysisCountChange?: (count: number) => void
  manageUrl?: boolean
}) {
  const { user } = useAuth()
  const mobile = useMobileViewport()
  const navigate = useNavigate()
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
  const [resolvingDuplicateId, setResolvingDuplicateId] = useState<number | null>(null)
  const [deletingBatch, setDeletingBatch] = useState(false)
  const [selectionMode, setSelectionMode] = useState(false)
  const [selectedTaskIds, setSelectedTaskIds] = useState<Set<number>>(new Set())
  const [selectingAll, setSelectingAll] = useState(false)
  const [publishingSelected, setPublishingSelected] = useState(false)
  const [publishRun, setPublishRun] = useState<PoliceDispatchPublishRun | null>(null)
  const [error, setError] = useState('')
  const taskRequestId = useRef(0)
  const announcedPublishRun = useRef<number | null>(null)
  const pendingResolvedDuplicateId = useRef<number | null>(null)

  const isSuperAdmin = Boolean(
    user?.permission_groups?.some(group => group.code === 'super_admin')
    || user?.permission_group?.code === 'super_admin'
    || (!user?.permission_groups?.length && user?.role === 'super_admin'),
  )

  const activeBatch = useMemo(
    () => batches.find(item => item.id === batchId) || null,
    [batches, batchId],
  )
  const selectedCount = selectedTaskIds.size
  const publishRunActive = publishRun?.status === 'pending' || publishRun?.status === 'running'

  const leavePublishSelection = () => {
    setSelectionMode(false)
    setSelectedTaskIds(new Set())
  }

  const isTaskPublishable = (item: PoliceDispatchTask) => (
    item.final_action === 'dispatch'
    && item.task_status === 'pending_publish'
    && ['pending', 'retryable'].includes(item.publish_status)
  )

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
      if (nextId && manageUrl) {
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

  const loadLatestPublishRun = async (targetBatchId = batchId) => {
    if (analysisOnly || !targetBatchId) {
      setPublishRun(null)
      return null
    }
    try {
      const run = await getLatestPoliceDispatchPublishRun(targetBatchId)
      setPublishRun(run)
      return run
    } catch {
      return null
    }
  }

  useEffect(() => { void loadHome() }, [])
  useEffect(() => {
    if (analysisOnly) onAnalysisCountChange?.(total)
  }, [analysisOnly, onAnalysisCountChange, total])
  useEffect(() => { if (batchId) void loadTasks(1) }, [batchId, status, category, appliedKeyword])
  useEffect(() => { void loadLatestPublishRun(batchId) }, [analysisOnly, batchId])
  useEffect(() => {
    if (!publishRunActive || !publishRun) return
    const runId = publishRun.id
    const timer = window.setInterval(async () => {
      try {
        const next = await getPoliceDispatchPublishRun(runId)
        setPublishRun(next)
        if (!['pending', 'running'].includes(next.status)) {
          window.clearInterval(timer)
          await Promise.all([loadHome(), loadTasks(page)])
          if (announcedPublishRun.current !== next.id) {
            announcedPublishRun.current = next.id
            message[next.status === 'completed' ? 'success' : 'warning'](
              next.status === 'completed'
                ? `后台发布完成：成功 ${next.success_count} 条`
                : `后台发布结束：成功 ${next.success_count} 条，另有 ${next.total_count - next.success_count} 条需要处理`,
            )
          }
        }
      } catch {
        // 短暂网络失败不终止后台任务，下次轮询继续读取。
      }
    }, 2000)
    return () => window.clearInterval(timer)
  }, [publishRun?.id, publishRunActive])
  useEffect(() => {
    const resolvedTaskId = pendingResolvedDuplicateId.current
    pendingResolvedDuplicateId.current = null
    setSelectedTaskIds(resolvedTaskId ? new Set([resolvedTaskId]) : new Set())
  }, [analysisOnly, batchId, status, category, appliedKeyword])

  useEffect(() => {
    if (!manageUrl || !batchId) return
    const next = new URLSearchParams(searchParams)
    next.set('batch', String(batchId))
    next.set('status', analysisOnly ? 'pending_review' : status)
    next.set('category', analysisOnly ? 'manual' : category)
    setSearchParams(next, { replace: true })
  }, [analysisOnly, batchId, category, manageUrl, setSearchParams, status])

  const changeBatch = (value: number) => {
    leavePublishSelection()
    setBatchId(value)
    const next = new URLSearchParams(searchParams)
    next.set('batch', String(value))
    next.set('status', analysisOnly ? 'pending_review' : status)
    next.set('category', analysisOnly ? 'manual' : category)
    setSearchParams(next, { replace: true })
    setPage(1)
  }

  const enterPublishSelection = () => {
    if (publishRunActive) {
      message.info('当前已有后台发布任务正在处理')
      return
    }
    setSelectionMode(true)
    setSelectedTaskIds(new Set())
    if (!['pending_publish', 'retryable', 'all'].includes(status)) {
      setStatus('pending_publish')
      setPage(1)
    }
  }

  const deleteActiveBatch = () => {
    if (!activeBatch || !isSuperAdmin) return
    Modal.confirm({
      title: `撤销批次 #${activeBatch.id}？`,
      content: (
        <div className="space-y-2 text-sm">
          <p>将删除该批次及其中 {activeBatch.total_count} 条审核、冲突和发布尝试记录，操作后不可恢复。</p>
          <p className="text-slate-500">这不会修改本地任务池中已经存在的同主键数据。只要本批次没有成功下发记录，即使当前显示内容冲突，也可以安全撤销。</p>
        </div>
      ),
      okText: '确认撤销',
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

  const selectAllPublishable = async () => {
    if (!batchId || selectingAll) return
    setSelectingAll(true)
    try {
      const result = await getPoliceDispatchPublishableSelection({
        batch_id: batchId,
        status,
        category,
        keyword: appliedKeyword,
      })
      setSelectedTaskIds(new Set(result.task_ids))
      if (!result.total) message.info('当前筛选结果中没有可发布任务')
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '全选当前筛选失败')
    } finally {
      setSelectingAll(false)
    }
  }

  const publishSelection = async () => {
    if (!activeBatch || !selectedCount || publishingSelected) return
    setPublishingSelected(true)
    try {
      const result = await publishSelectedPoliceDispatchTasks(
        activeBatch.id,
        [...selectedTaskIds],
      )
      setPublishRun(result)
      announcedPublishRun.current = null
      message.success(result.message)
      leavePublishSelection()
      await loadTasks(1)
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '所选任务发布失败')
      await loadLatestPublishRun(activeBatch.id)
      await loadTasks(page)
    } finally {
      setPublishingSelected(false)
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

  const adoptExistingContent = async () => {
    if (!selected?.linked_row_hash) return
    setSaving(true)
    try {
      const result = await adoptExistingPoliceDispatchContent(selected.id, {
        expected_version: selected.version,
        expected_row_hash: selected.linked_row_hash,
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

  const keepDuplicateTask = async (keepTaskId: number) => {
    if (duplicates.length < 2 || resolvingDuplicateId) return
    setResolvingDuplicateId(keepTaskId)
    try {
      const result = await resolvePoliceDispatchDuplicateGroup(keepTaskId, {
        tasks: duplicates.map(item => ({ id: item.id, version: item.version })),
        review_note: '重复组人工选择保留项',
      })
      message.success(`${result.message}；保留项已选中，可直接发布`)
      setSelected(null)
      setSelectionMode(true)
      if (!['pending_publish', 'retryable', 'all'].includes(status)) {
        pendingResolvedDuplicateId.current = result.keep_task_id
        setStatus('pending_publish')
        setPage(1)
        await loadHome()
      } else {
        setSelectedTaskIds(new Set([result.keep_task_id]))
        await Promise.all([loadHome(), loadTasks(1)])
      }
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '重复任务处理失败')
      const refreshed = await getPoliceDispatchTask(keepTaskId).catch(() => null)
      if (refreshed) {
        setSelected(refreshed.task)
        setDuplicates(refreshed.duplicates)
        setDuplicateDifferences(refreshed.duplicate_differences || [])
      }
    } finally {
      setResolvingDuplicateId(null)
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
  const publishStatusActive = publishStatusValues.includes(status)
  const selectedBusinessIsFullchain = selected?.target_parser === '全链条' || !selected?.target_parser
  const selectedDisplayName = selected
    ? selected.person_name
      || selected.standard_values?.['姓名']
      || selected.standard_values?.['接警编号']
      || `Excel 第 ${selected.source_row} 行`
    : '待核查对象'
  const completedCount = activeBatch
    ? Math.max(0, activeBatch.counts.total - activeBatch.counts.pending_review - activeBatch.counts.pending_publish)
    : 0
  const unpublishedCount = activeBatch
    ? Math.max(
        0,
        activeBatch.counts.pending_publish
          - activeBatch.counts.retryable
          - activeBatch.counts.needs_reconciliation
          - activeBatch.counts.conflict,
      )
    : 0
  const selectTaskFilter = (nextStatus: string, nextCategory = 'all') => {
    leavePublishSelection()
    setStatus(nextStatus)
    setCategory(nextCategory)
    setPage(1)
  }

  return (
    <div className="police-dispatch-workbench mx-auto max-w-7xl space-y-4 pb-4">
      <section className="police-dispatch-workbench__hero app-card overflow-hidden p-5 shadow-lg">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="police-dispatch-workbench__eyebrow text-xs font-medium">内勤业务 · 共享队列</div>
            <h1 className="mt-1 text-xl font-semibold">{analysisOnly ? '未下发数据研判' : '下发任务处理'}</h1>
          </div>
          <Button className="police-dispatch-workbench__refresh" ghost icon={<ReloadOutlined />} onClick={() => Promise.all([loadHome(), loadTasks(page)])}>刷新</Button>
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
          {!analysisOnly && activeBatch && (
            <Button
              ghost
              icon={<FileSearchOutlined />}
              className="shrink-0"
              onClick={() => navigate(`/police-dispatch/batches/${activeBatch.id}`)}
            >
              <span className="hidden sm:inline">批次详情</span>
            </Button>
          )}
          {isSuperAdmin && activeBatch && (
            <Tooltip
              title={activeBatch.counts.published > 0
                ? `已有 ${activeBatch.counts.published} 条任务成功进入本地任务池，需保留批次记录`
                : '撤销未成功下发的批次；内容冲突和发布尝试记录会一并清理'}
            >
              <span className="shrink-0">
                <Button
                  danger
                  icon={<DeleteOutlined />}
                  loading={deletingBatch}
                  disabled={activeBatch.counts.published > 0}
                  onClick={deleteActiveBatch}
                >
                  <span className="hidden sm:inline">撤销批次</span>
                </Button>
              </span>
            </Tooltip>
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
            {!analysisOnly ? (
              <div className="police-dispatch-status-filter-grid mt-3" role="tablist" aria-label="任务状态">
                <div className="police-dispatch-status-filter-group">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={status === 'pending_review'}
                    className={`police-dispatch-status-metric${status === 'pending_review' ? ' is-active' : ''}`}
                    onClick={() => selectTaskFilter('pending_review')}
                  >
                    <span className="police-dispatch-status-metric__count">{activeBatch.counts.pending_review}</span>
                    <span className="police-dispatch-status-metric__label">待审核</span>
                  </button>
                  <div className="police-dispatch-status-children police-dispatch-status-children--review">
                    {[
                      { label: '重复', value: 'duplicate', count: activeBatch.counts.duplicate },
                      { label: '待研判', value: 'manual', count: activeBatch.counts.abnormal },
                    ].map(option => (
                      <button
                        key={option.value}
                        type="button"
                        className={`police-dispatch-status-chip${status === 'pending_review' && category === option.value ? ' is-active' : ''}`}
                        aria-pressed={status === 'pending_review' && category === option.value}
                        onClick={() => selectTaskFilter('pending_review', option.value)}
                      >
                        <span>{option.label}</span>
                        <strong>{option.count}</strong>
                      </button>
                    ))}
                  </div>
                </div>

                <div className="police-dispatch-status-filter-group">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={publishStatusActive}
                    className={`police-dispatch-status-metric${publishStatusActive ? ' is-active' : ''}`}
                    onClick={() => selectTaskFilter('pending_publish')}
                  >
                    <span className="police-dispatch-status-metric__count">{activeBatch.counts.pending_publish}</span>
                    <span className="police-dispatch-status-metric__label">待发布</span>
                  </button>
                  <div className="police-dispatch-status-children police-dispatch-status-children--publish">
                    {publishStatusOptions.map(option => {
                      const count = option.value === 'pending_publish'
                        ? unpublishedCount
                        : activeBatch.counts[option.value]
                      const chip = (
                        <button
                          key={option.value}
                          type="button"
                          className={`police-dispatch-status-chip${status === option.value ? ' is-active' : ''}`}
                          aria-pressed={status === option.value}
                          onClick={() => selectTaskFilter(option.value)}
                        >
                          <span>{option.label}</span>
                          <strong>{count}</strong>
                        </button>
                      )
                      return option.value === 'needs_reconciliation'
                        ? <Tooltip key={option.value} title={reconciliationHint}>{chip}</Tooltip>
                        : chip
                    })}
                  </div>
                  {status === 'needs_reconciliation' && (
                    <div className="police-dispatch-status-hint">{reconciliationHint}</div>
                  )}
                </div>

                {[
                  { label: '已完成', value: 'completed', count: completedCount },
                  { label: '全部', value: 'all', count: activeBatch.counts.total },
                ].map(option => (
                  <div key={option.value} className="police-dispatch-status-filter-group">
                    <button
                      type="button"
                      role="tab"
                      aria-selected={status === option.value}
                      className={`police-dispatch-status-metric${status === option.value ? ' is-active' : ''}`}
                      onClick={() => selectTaskFilter(option.value)}
                    >
                      <span className="police-dispatch-status-metric__count">{option.count}</span>
                      <span className="police-dispatch-status-metric__label">{option.label}</span>
                    </button>
                  </div>
                ))}
              </div>
            ) : (
              <div className="mobile-task-priority-grid police-dispatch-analysis-counts" aria-label="未下发研判数量">
                {[
                  ['待审核', activeBatch.counts.pending_review, false],
                  ['待研判', activeBatch.counts.abnormal, true],
                  ['重复', activeBatch.counts.duplicate, false],
                  ['全部', activeBatch.counts.total, false],
                ].map(([label, value, active]) => (
                  <div key={String(label)} className={`mobile-task-priority-card${active ? ' is-active' : ''}`}>
                    <span>{label}</span>
                    <strong>{value}</strong>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </section>

      {error && <Alert type="error" showIcon message={error} />}

      <section className="app-card p-3 sm:p-4">
        {analysisOnly && (
          <Alert
            type="info"
            showIcon
            message="这里处理尚未下发、无法直接确定去向的数据；研判结果仍保存在原下发批次。"
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
            {!analysisOnly && activeBatch && !selectionMode && (
              <Button
                type="primary"
                icon={<CheckSquareOutlined />}
                onClick={enterPublishSelection}
                disabled={publishRunActive}
              >
                {publishRunActive ? '正在后台发布' : '选择发布'}
              </Button>
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

      {!analysisOnly && publishRun && (
        <section className={`app-card police-dispatch-publish-run${publishRunActive ? ' is-active' : ''}`}>
          <div className="police-dispatch-publish-run__header">
            <div>
              <div className="text-sm font-semibold text-[var(--app-text-strong)]">
                {publishRunActive ? '后台发布进行中' : '最近一次发布'} · #{publishRun.id}
              </div>
              <div className="mt-1 text-xs text-[var(--app-text-secondary)]">
                {publishRunActive
                  ? '可以离开本页面，服务器会继续处理；返回后仍可查看进度。'
                  : publishRun.error_message || '发布任务已经结束。'}
              </div>
            </div>
            <Tag color={publishRun.status === 'completed' ? 'success' : publishRunActive ? 'processing' : 'warning'}>
              {publishRun.status === 'pending' ? '等待开始'
                : publishRun.status === 'running' ? '处理中'
                  : publishRun.status === 'completed' ? '已完成'
                    : publishRun.status === 'partial' ? '部分完成' : '已停止'}
            </Tag>
          </div>
          <Progress
            className="mt-3"
            percent={publishRun.total_count
              ? Math.round(publishRun.processed_count / publishRun.total_count * 100)
              : 0}
            status={publishRun.status === 'failed' ? 'exception' : undefined}
            format={() => `${publishRun.processed_count}/${publishRun.total_count}`}
          />
          <div className="police-dispatch-publish-run__counts">
            <span>成功 <strong>{publishRun.success_count}</strong></span>
            <span>冲突 <strong>{publishRun.conflict_count}</strong></span>
            <span>待对账 <strong>{publishRun.reconciliation_count}</strong></span>
            <span>可重试 <strong>{publishRun.retryable_count}</strong></span>
          </div>
        </section>
      )}

      {!analysisOnly && selectionMode && (
        <section className="app-card mobile-task-bulk-toolbar is-sticky">
          <div className="flex flex-wrap items-center gap-2">
            <Button size="small" onClick={leavePublishSelection}>退出选择</Button>
            <Button size="small" loading={selectingAll} onClick={selectAllPublishable}>
              全选当前筛选
            </Button>
            <Button
              size="small"
              disabled={!selectedCount}
              onClick={() => setSelectedTaskIds(new Set())}
            >
              清空
            </Button>
            <span className="text-sm font-medium text-[var(--app-text)]">已选 {selectedCount} 条</span>
            <Popconfirm
              title={`发布选中的 ${selectedCount} 条任务？`}
              description="只发布当前明确选中的已审核任务；发布前会再次校验状态和重复人员组。"
              okText="确认发布"
              cancelText="取消"
              disabled={!selectedCount || publishRunActive}
              onConfirm={publishSelection}
            >
              <Button
                type="primary"
                size="small"
                icon={<SendOutlined />}
                loading={publishingSelected}
                disabled={!selectedCount || publishRunActive}
              >
                发布所选
              </Button>
            </Popconfirm>
          </div>
          <div className="mt-1 text-xs text-[var(--app-text-secondary)]">
            直接点击卡片多选；“全选当前筛选”会包含其他分页中的可发布任务。
          </div>
        </section>
      )}

      <Spin spinning={loading}>
        <div className="mobile-task-list">
          {tasks.map(item => {
            const selectable = isTaskPublishable(item)
            const isSelected = selectedTaskIds.has(item.id)
            const openOrSelect = () => {
              if (!selectionMode) {
                void openTask(item)
                return
              }
              if (!selectable) {
                message.info('该任务当前不可发布')
                return
              }
              setSelectedTaskIds(current => {
                const next = new Set(current)
                if (next.has(item.id)) next.delete(item.id)
                else next.add(item.id)
                return next
              })
            }
            return (
              <TaskCard
                key={item.id}
                item={item}
                selectionMode={selectionMode}
                selected={isSelected}
                selectable={selectable}
                onOpen={openOrSelect}
              />
            )
          })}
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
        title={selected ? `${selectedDisplayName} · 第 ${selected.source_row} 行` : '任务审核'}
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
                  该条已经审核，无需逐条保存；请关闭详情后进入“选择发布”进行多选。
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
                      {(label === '身份证号' || label === '手机号' || label === '原地址') && (
                        <CopyIconButton value={value} label={label} />
                      )}
                    </div>
                  </div>
                ) : null)}
              </section>

              {selectedBusinessIsFullchain ? <section className="rounded-2xl border border-slate-200 p-4">
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
              </section> : (
                <section className="rounded-2xl border border-blue-200 bg-blue-50 p-4">
                  <div className="font-medium text-blue-900">业务标准字段</div>
                  <div className="mt-1 text-xs text-blue-700">该业务按独立适配器导入，标准字段只读；如需修正，请重新上传修正后的文件。</div>
                  <div className="mt-3 grid gap-2 sm:grid-cols-2">
                    {Object.entries(selected.standard_values || {}).map(([field, value]) => (
                      <div key={field} className="rounded-lg bg-white/80 p-2 text-sm">
                        <span className="text-slate-500">{field}：</span>
                        <span className="break-all text-slate-800">{value || '（空）'}</span>
                      </div>
                    ))}
                  </div>
                  {selected.validation_issues.length > 0 && (
                    <Alert
                      className="mt-3"
                      type="warning"
                      showIcon
                      message="该行存在导入问题"
                      description={selected.validation_issues.map(issue => `${issue.field}：${issue.value}`).join('；')}
                    />
                  )}
                </section>
              )}

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
                      <div
                        key={item.task_id}
                        className={`w-full rounded-xl border p-3 text-left text-xs ${item.task_id === selected.id ? 'border-orange-500 bg-white' : 'border-orange-200 bg-orange-50/60'}`}
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div className="font-medium">Excel 第 {item.source_row} 行</div>
                          <Popconfirm
                            title={`保留 Excel 第 ${item.source_row} 行？`}
                            description="该条将保留为待发布任务，同一身份证号的其余记录会同时标记为重复排除。"
                            okText="保留此条"
                            cancelText="取消"
                            onConfirm={() => void keepDuplicateTask(item.task_id)}
                          >
                            <Button
                              size="small"
                              type={item.task_id === selected.id ? 'primary' : 'default'}
                              loading={resolvingDuplicateId === item.task_id}
                              disabled={resolvingDuplicateId !== null}
                            >
                              保留此条
                            </Button>
                          </Popconfirm>
                        </div>
                        <div className="mt-2 space-y-1.5">
                          {item.fields.map(field => (
                            <div key={field.field} className="grid grid-cols-[88px_minmax(0,1fr)] gap-2">
                              <span className="text-slate-400">{field.field}</span>
                              <span className="break-all text-slate-700">{field.value || '（空）'}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                </section>
              )}

              {selected.publish_status === 'conflict' && (
                <section className="rounded-2xl border border-red-200 bg-red-50 p-4">
                  <div className="font-medium text-red-900">本地现有内容与待发布内容不同</div>
                  <div className="mt-3 space-y-2">
                    {selected.conflict_diff.map(item => (
                      <div key={item.field} className="rounded-xl bg-white p-3 text-xs">
                        <div className="font-medium text-slate-700">{item.field}</div>
                        <div className="mt-2 grid gap-2 sm:grid-cols-2">
                          <div><span className="text-slate-400">待发布：</span><span className="break-all">{item.platform || '（空）'}</span></div>
                          <div><span className="text-slate-400">本地现有：</span><span className="break-all">{item.tencent || '（空）'}</span></div>
                        </div>
                      </div>
                    ))}
                  </div>
                  <div className="mt-4 flex flex-wrap gap-2">
                    <Button onClick={() => void adoptExistingContent()}>采用本地现有内容</Button>
                    <span className="self-center text-xs text-slate-500">如需使用待发布内容，请先修改或撤回当前任务后重新发布。</span>
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
