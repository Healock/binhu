import {
  ArrowLeftOutlined,
  CameraOutlined,
  DownloadOutlined,
  SafetyCertificateOutlined,
  PhoneOutlined,
  SaveOutlined,
} from '@ant-design/icons'
import {
  Alert,
  Button,
  Collapse,
  Descriptions,
  Empty,
  Image,
  Input,
  Modal,
  Select,
  Skeleton,
  Tag,
  message,
} from 'antd'
import { useNavigate, useParams } from 'react-router-dom'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  getMobileTaskDetail,
  getMobileTaskAnalysisDetail,
  getQmfRegistrationRun,
  executeQmfRegistration,
  prepareQmfRegistration,
  retryQmfTencentMarker,
  resolveMobileTaskSyncConflict,
  updateMobileTask,
  updateMobileTaskAnalysis,
  workflowApi,
  type MobileTaskDetailData,
  type MobileTaskSource,
  type QmfPrepareResult,
  type QmfRegistrationRun,
} from '../api/client'
import { useAuth } from '../context/AuthContext'
import {
  confirmPendingNavigation,
  setPendingNavigationChanges,
} from '../utils/navigationGuard'
import {
  buildMobileTaskChanges,
  mergeMobileTaskSaveValues,
  mobileTaskCanLaunchTelephone,
  mobileTaskEditorFields,
  mobileTaskPhoneOptions,
  mobileTaskSourceTags,
  mobileTaskSourceDifferences,
  mobileTaskSourceNeedsReview,
  mobileTaskSourceState,
} from '../utils/mobileTasks'
import MobilePhonePicker from '../components/MobilePhonePicker'
import useMobileViewport from '../hooks/useMobileViewport'
import useSystemTime from '../hooks/useSystemTime'
import {
  QMF_MARKER_STATUS,
  QMF_RUN_STATUS,
  QMF_STEP_STATUS,
  canExecutePreparedQmfRun,
  qmfRunCanReprepare,
  qmfRunIsPolling,
} from '../utils/qmfRegistration'

const STATE_LABELS = {
  unchecked: { text: '未核查', color: 'red' },
  checked: { text: '待补结果', color: 'orange' },
  completed: { text: '已完成', color: 'green' },
} as const

const SYNC_LABELS = {
  pending: { text: '待同步', color: 'blue' },
  retry: { text: '同步重试', color: 'orange' },
  conflict: { text: '同步冲突', color: 'red' },
} as const

function firstValue(values: Record<string, string>, fields: string[]) {
  for (const field of fields) {
    if (values[field]?.trim()) return values[field].trim()
  }
  return ''
}

function detailError(reason: any, fallback: string) {
  const detail = reason?.response?.data?.detail
  return typeof detail === 'object' ? detail?.message || fallback : detail || reason?.message || fallback
}

export default function MobileTaskDetail({ mode = 'tasks' }: { mode?: 'tasks' | 'analysis' }) {
  const navigate = useNavigate()
  const mobile = useMobileViewport()
  const formatSystemTime = useSystemTime()
  const { recordActivity, user } = useAuth()
  const { parserType = '', rowKey = '' } = useParams()
  const [data, setData] = useState<MobileTaskDetailData | null>(null)
  const [selectedSourceId, setSelectedSourceId] = useState<number | null>(null)
  const [formValues, setFormValues] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [resolvingConflict, setResolvingConflict] = useState('')
  const [error, setError] = useState('')
  const [savedMessage, setSavedMessage] = useState('')
  const [photoRequestOpen, setPhotoRequestOpen] = useState(false)
  const [photoSubmitting, setPhotoSubmitting] = useState(false)
  const [qmfPreviewOpen, setQmfPreviewOpen] = useState(false)
  const [qmfPreviewLoading, setQmfPreviewLoading] = useState(false)
  const [qmfPrepareResult, setQmfPrepareResult] = useState<QmfPrepareResult | null>(null)
  const [qmfRun, setQmfRun] = useState<QmfRegistrationRun | null>(null)
  const [qmfExecuting, setQmfExecuting] = useState(false)
  const [qmfMarkerRetrying, setQmfMarkerRetrying] = useState(false)
  const [qmfPreviewError, setQmfPreviewError] = useState('')
  const qmfPreviewRequestActive = useRef(false)

  const selectedSource = useMemo(
    () => data?.sources.find(source => source.id === selectedSourceId) || null,
    [data, selectedSourceId],
  )
  const visibleEditorFields = useMemo(() => (
    data && selectedSource
      ? mobileTaskEditorFields(
          data,
          selectedSource.editable_fields,
          formValues,
          selectedSource.values,
        )
      : []
  ), [data, formValues, selectedSource])
  const preservedSecondaryFeedback = useMemo(() => (
    data && selectedSource
      ? data.workflow.secondary_fields
          .filter(field => !visibleEditorFields.includes(field))
          .map(field => ({ field, value: selectedSource.values[field]?.trim() || '' }))
          .filter(item => item.value)
      : []
  ), [data, selectedSource, visibleEditorFields])
  const changes = useMemo(() => {
    if (!selectedSource) return {}
    return buildMobileTaskChanges(
      selectedSource.values,
      formValues,
      visibleEditorFields,
    )
  }, [formValues, selectedSource, visibleEditorFields])
  const sourceDifferences = useMemo(() => (
    data ? mobileTaskSourceDifferences(data.sources, data.workflow.columns) : []
  ), [data])
  const dirty = Object.keys(changes).length > 0

  const selectSource = useCallback((source: MobileTaskSource) => {
    setSelectedSourceId(source.id)
    setFormValues({ ...source.values })
    setSavedMessage('')
  }, [])

  const load = useCallback(async (preferredSourceId?: number) => {
    setLoading(true)
    setError('')
    try {
      const result = mode === 'analysis'
        ? await getMobileTaskAnalysisDetail(parserType, rowKey)
        : await getMobileTaskDetail(parserType, rowKey)
      setData(result)
      const source = result.sources.find(item => item.id === preferredSourceId) || result.sources[0]
      if (source) selectSource(source)
    } catch (reason: any) {
      setError(detailError(reason, '任务详情读取失败'))
    } finally {
      setLoading(false)
    }
  }, [mode, parserType, rowKey, selectSource])

  useEffect(() => { void load() }, [load])

  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (!dirty) return
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  useEffect(() => {
    setPendingNavigationChanges(dirty)
    return () => setPendingNavigationChanges(false)
  }, [dirty])

  const chooseSource = (source: MobileTaskSource) => {
    if (source.id === selectedSourceId) return
    if (dirty && !window.confirm('切换来源会丢失当前未保存内容，确定切换吗？')) return
    selectSource(source)
  }

  const save = async () => {
    if (!selectedSource || !dirty) return
    setSaving(true)
    setError('')
    setSavedMessage('')
    try {
      const updater = mode === 'analysis' ? updateMobileTaskAnalysis : updateMobileTask
      const result = await updater(parserType, selectedSource.id, {
        changes,
        base_values: Object.fromEntries(
          Object.keys(changes).map(field => [field, selectedSource.values[field] || '']),
        ),
        expected_revision: selectedSource.revision,
      })
      const savedValues = mergeMobileTaskSaveValues(
        selectedSource.values,
        changes,
        result.values,
        selectedSource.cell_meta,
      )
      setData(current => current ? {
        ...current,
        task: {
          ...current.task,
          pending_sync: true,
          sync_state: result.sync_state,
          review_stage: mode === 'analysis'
            ? (firstValue(savedValues, current.workflow.analysis_fields) ? 'analyzed' : 'waiting_analysis')
            : current.task.review_stage,
          summary: mode === 'analysis' ? {
            ...current.task.summary,
            analysis: firstValue(savedValues, current.workflow.analysis_fields),
          } : current.task.summary,
        },
        sources: current.sources.map(source => source.id === selectedSource.id ? {
          ...source,
          values: savedValues,
          revision: result.revision,
          sync_state: result.sync_state,
          sync_fields: [
            ...source.sync_fields.filter(item => !(item.field in changes)),
            ...Object.entries(changes).map(([field, platformValue]) => ({
              field,
              platform_value: platformValue,
              tencent_value: null,
              status: 'pending' as const,
              error_code: '',
            })),
          ],
          state: mobileTaskSourceState(
            parserType,
            current.workflow.result_field,
            savedValues,
          ),
          needs_review: mobileTaskSourceNeedsReview(
            current.workflow.result_field,
            current.workflow.secondary_fields,
            savedValues,
          ),
          review_stage: mode === 'analysis'
            ? (firstValue(savedValues, current.workflow.analysis_fields) ? 'analyzed' : 'waiting_analysis')
            : source.review_stage,
        } : source),
      } : current)
      setFormValues(savedValues)
      setSavedMessage(result.message)
    } catch (reason: any) {
      const status = reason?.response?.status
      setError(detailError(reason, '保存失败，请稍后重试'))
      if (status === 409 || status === 502) await load(selectedSource.id)
    } finally {
      setSaving(false)
    }
  }

  const resolveConflict = async (field: string, choice: 'platform' | 'tencent') => {
    if (!selectedSource || dirty) return
    const key = `${field}:${choice}`
    setResolvingConflict(key)
    setError('')
    try {
      const result = await resolveMobileTaskSyncConflict(parserType, selectedSource.id, {
        choice,
        fields: [field],
      })
      message.success(result.message)
      await load(selectedSource.id)
    } catch (reason: any) {
      setError(detailError(reason, '同步冲突处理失败'))
    } finally {
      setResolvingConflict('')
    }
  }

  const copy = async (value: string, label: string) => {
    try {
      await navigator.clipboard.writeText(value)
      message.success(`${label}已复制`)
    } catch {
      message.error(`${label}复制失败，请长按或选中文字复制`)
    }
  }

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

  const submitPhotoRequest = async () => {
    if (!identityNumber || !title.trim()) return
    setPhotoSubmitting(true)
    try {
      await workflowApi.createTicket({
        type_code: 'photo_request',
        title: `${title.trim()}照片调取`,
        description: '',
        priority: 'normal',
        form_data: {
          subject_type: 'task',
          subject_id: rowKey,
          subject_name: title.trim(),
          identity_number: identityNumber,
          request_reason: '',
          source_parser_type: parserType,
          source_row_key: rowKey,
          community_name: selectedSource?.values.社区 || data?.task.community || '',
          source_label: data?.workflow.label || parserType,
        },
        links: [{ object_type: 'mobile_task', object_id: `${parserType}:${rowKey}` }],
      })
      message.success('照片调取工单已提交')
      setPhotoRequestOpen(false)
    } catch (reason: any) {
      message.error(detailError(reason, '照片调取工单提交失败'))
    } finally {
      setPhotoSubmitting(false)
    }
  }

  const openQmfRegistration = async () => {
    if (
      qmfPreviewRequestActive.current
      || !selectedSource
      || !data.qmf_registration?.enabled
      || dirty
    ) return
    qmfPreviewRequestActive.current = true
    setQmfPreviewOpen(true)
    setQmfPreviewLoading(true)
    setQmfPrepareResult(null)
    setQmfRun(null)
    setQmfPreviewError('')
    try {
      const result = await prepareQmfRegistration({
        parser_type: parserType,
        row_key: rowKey,
        source_id: selectedSource.id,
        expected_revision: selectedSource.revision,
      })
      setQmfPrepareResult(result)
      setQmfRun(result.run)
    } catch (reason: any) {
      setQmfPreviewError(detailError(reason, '全民防登记准备失败，请稍后重试'))
    } finally {
      qmfPreviewRequestActive.current = false
      setQmfPreviewLoading(false)
    }
  }

  const openExistingQmfRun = () => {
    const latest = data.qmf_registration?.latest_run
    if (!latest) return
    setQmfPreviewOpen(true)
    setQmfPrepareResult(null)
    setQmfRun(latest)
    setQmfPreviewError('')
  }

  const executePreparedQmfRun = () => {
    if (!canExecutePreparedQmfRun(qmfRun, Boolean(qmfPrepareResult))) return
    Modal.confirm({
      title: '最后确认：执行全民防登记？',
      content: '此操作会依次上传照片、保存人员资料并反馈模型三，提交后不能撤销。任何不确定结果都会冻结本次运行。',
      okText: '确认执行',
      cancelText: '取消',
      onOk: async () => {
        if (!qmfRun) return
        setQmfExecuting(true)
        setQmfPreviewError('')
        try {
          const next = await executeQmfRegistration(qmfRun.id)
          setQmfRun(next)
          setData(current => current?.qmf_registration ? {
            ...current,
            qmf_registration: { ...current.qmf_registration, latest_run: next },
          } : current)
        } catch (reason: any) {
          setQmfPreviewError(detailError(reason, '全民防登记启动失败'))
        } finally {
          setQmfExecuting(false)
        }
      },
    })
  }

  const retryQmfMarker = async () => {
    if (!qmfRun?.can_retry_marker || qmfMarkerRetrying) return
    setQmfMarkerRetrying(true)
    setQmfPreviewError('')
    try {
      const next = await retryQmfTencentMarker(qmfRun.id)
      setQmfRun(next)
      message.success('腾讯完成标记已写入')
      await load(selectedSourceId || undefined)
    } catch (reason: any) {
      setQmfPreviewError(detailError(reason, '腾讯完成标记重试失败'))
    } finally {
      setQmfMarkerRetrying(false)
    }
  }

  useEffect(() => {
    if (!qmfRunIsPolling(qmfRun)) return
    let cancelled = false
    let timer = 0
    const poll = async () => {
      try {
        const next = await getQmfRegistrationRun(qmfRun!.id)
        if (cancelled) return
        setQmfRun(next)
        setData(current => current?.qmf_registration ? {
          ...current,
          qmf_registration: { ...current.qmf_registration, latest_run: next },
        } : current)
        if (qmfRunIsPolling(next)) {
          timer = window.setTimeout(poll, 1000)
        } else {
          await load(selectedSourceId || undefined)
        }
      } catch (reason: any) {
        if (!cancelled) {
          setQmfPreviewError(detailError(reason, '全民防登记进度读取失败'))
          timer = window.setTimeout(poll, 2500)
        }
      }
    }
    timer = window.setTimeout(poll, 700)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [load, qmfRun?.id, qmfRun?.status, selectedSourceId])

  if (loading && !data) {
    return <div className="app-card p-5"><Skeleton active paragraph={{ rows: 10 }} /></div>
  }

  if (!data) {
    return <div className="mobile-task-page"><Alert type="error" showIcon message={error || '任务不存在'} /><Button icon={<ArrowLeftOutlined />} onClick={() => navigate(-1)}>返回任务列表</Button></div>
  }

  const state = STATE_LABELS[selectedSource?.state || data.task.state]
  const title = selectedSource
    ? firstValue(selectedSource.values, data.workflow.title_fields)
    : data.task.summary.title
  const inspector = selectedSource?.values.核查人 || data.task.inspector
  const taskDate = selectedSource
    ? firstValue(selectedSource.values, data.workflow.date_fields)
    : data.task.summary.date
  const phone = selectedSource ? firstValue(selectedSource.values, data.workflow.phone_fields) : data.task.summary.phone
  const identityNumber = selectedSource
    ? firstValue(selectedSource.values, data.workflow.identity_fields)
    : data.task.summary.identity_number
  const source = selectedSource
    ? firstValue(selectedSource.values, data.workflow.source_fields)
    : data.task.summary.source
  const currentAddress = selectedSource?.values.现住址?.trim() || ''
  const originalAddress = selectedSource
    ? firstValue(
        selectedSource.values,
        data.workflow.address_fields.filter(field => field !== '现住址'),
      )
    : data.task.summary.address
  const analysis = selectedSource
    ? firstValue(selectedSource.values, data.workflow.analysis_fields)
    : data.task.summary.analysis
  const phoneOptions = mobileTaskPhoneOptions(phone)
  const phoneDisplay = phoneOptions.length > 0 ? phoneOptions.join('、') : phone
  const sourceTags = mobileTaskSourceTags(source)
  const syncState = selectedSource?.sync_state || data.task.sync_state
  const syncLabel = syncState ? SYNC_LABELS[syncState] : null
  const conflictFields = selectedSource?.sync_fields.filter(item => item.status === 'conflict') || []
  const detailFacts = [
    { label: '身份证号', value: identityNumber || '未填写', copyValue: identityNumber, copyLabel: '身份证号' },
    { label: '手机号', value: phoneDisplay || '未填写', phones: phoneOptions },
    ...(originalAddress
      ? [{ label: currentAddress ? '原地址' : '地址', value: originalAddress, wide: true, copyValue: originalAddress, copyLabel: currentAddress ? '原地址' : '地址' }]
      : []),
    ...(currentAddress
      ? [{ label: '现住址', value: currentAddress, wide: true, copyValue: currentAddress, copyLabel: '现住址' }]
      : []),
    ...(!originalAddress && !currentAddress
      ? [{ label: '地址', value: '未填写', wide: true }]
      : []),
  ]
  const latestQmfRun = qmfRun || data.qmf_registration?.latest_run || null
  const canReprepareQmfRun = qmfRunCanReprepare(qmfRun)
  const shouldResumeQmfRun = Boolean(
    latestQmfRun
    && ['prepared', 'executing', 'succeeded', 'failed', 'uncertain'].includes(latestQmfRun.status),
  )

  return (
    <div className="mobile-task-page mobile-task-detail-page">
      <div className="flex items-center justify-between gap-3">
        <Button type="text" className="min-h-11 px-1" icon={<ArrowLeftOutlined />} onClick={() => { if (confirmPendingNavigation()) navigate(-1) }}>返回</Button>
        <div className="flex items-center gap-2">
          <Tag color={state.color}>{state.text}</Tag>
          {syncLabel && <Tag color={syncLabel.color}>{syncLabel.text}</Tag>}
        </div>
      </div>

      <section className="app-card p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="text-xs text-[var(--app-text-secondary)]">{data.task.parser_type}</div>
            <h1 className="mt-1 text-xl font-semibold text-[var(--app-text-strong)]">{title || '未填写姓名'}</h1>
            <p className="mt-1 text-sm text-[var(--app-text-secondary)]">{data.task.community} · {inspector || '待分配'}</p>
            {taskDate && <p className="mt-1 text-xs text-[var(--app-text-muted)]">任务时间 {taskDate}</p>}
          </div>
          {(selectedSource?.needs_review || data.task.conflict) && <Tag color="warning">需复核</Tag>}
        </div>

        {data.task.watch_marks?.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-2">
            {data.task.watch_marks.map(mark => (
              <Tag key={mark.category_id} color={mark.color}>
                {mark.name}{mark.source_type ? ` · ${mark.source_type}` : ''}
              </Tag>
            ))}
          </div>
        )}
        {data.task.first_dispatch_at && (
          <p className="mt-2 text-xs text-[var(--app-text-muted)]">首次下发：{data.task.first_dispatch_at}</p>
        )}

        <dl className="mobile-task-detail-facts">
          {detailFacts.map(fact => (
            <div
              key={fact.label}
              className={`mobile-task-detail-facts__item${fact.wide ? ' is-wide' : ''}`}
            >
              <dt>{fact.label}</dt>
              <dd>
                {'phones' in fact && fact.phones?.length ? (
                  <MobilePhonePicker
                    phones={fact.phones}
                    mode="copy"
                    label={<span>{fact.value}</span>}
                    buttonProps={{ type: 'text', className: 'mobile-task-detail-copy-value' }}
                    onSelect={value => void copy(value, '手机号')}
                  />
                ) : 'copyValue' in fact && fact.copyValue ? (
                  <button
                    type="button"
                    className="mobile-task-detail-copy-value"
                    onClick={() => void copy(fact.copyValue || '', fact.copyLabel || fact.label)}
                  >{fact.value}</button>
                ) : fact.value}
              </dd>
            </div>
          ))}
        </dl>
        {sourceTags.length > 0 && (
          <div className="mobile-task-source-cloud mobile-task-source-cloud--detail">
            <span>来源</span>
            <div>
              {sourceTags.map(tag => (
                <Tag key={tag} className="mobile-task-source-cloud__tag">{tag}</Tag>
              ))}
            </div>
          </div>
        )}

        <div className="mobile-task-detail-primary-actions">
          <MobilePhonePicker
            phones={phoneOptions}
            mode="dial"
            label={phoneOptions.length > 1 ? `选择拨打（${phoneOptions.length}）` : '拨打电话'}
            buttonProps={{ className: 'mobile-task-detail-pill', type: 'primary', icon: <PhoneOutlined /> }}
            onSelect={value => void dial(value)}
          />
          {phoneOptions.length === 0 && (
            <Button disabled className="mobile-task-detail-pill" icon={<PhoneOutlined />}>缺少电话号码</Button>
          )}
          {user?.permissions.includes('workflow.ticket.create') && (
            <Button
              className="mobile-task-detail-pill"
              icon={<CameraOutlined />}
              disabled={!identityNumber}
              onClick={() => setPhotoRequestOpen(true)}
            >{identityNumber ? '调取照片' : '缺少身份证号'}</Button>
          )}
          {mode === 'tasks' && data.qmf_registration?.visible && (
            <Button
              type="primary"
              className="mobile-task-detail-pill"
              icon={<SafetyCertificateOutlined />}
              disabled={
                (!shouldResumeQmfRun && !data.qmf_registration.enabled)
                || !selectedSource?.source_available
                || dirty
                || qmfPreviewLoading
              }
              title={dirty
                ? '请先保存或放弃当前修改'
                : !selectedSource?.source_available
                  ? '腾讯来源行已不存在，不能准备登记'
                  : data.qmf_registration.reason}
              onClick={() => {
                if (shouldResumeQmfRun) openExistingQmfRun()
                else void openQmfRegistration()
              }}
            >{shouldResumeQmfRun ? '查看全民防登记记录' : '全民防登记'}</Button>
          )}
        </div>
        {analysis && (
          <div className="mobile-task-analysis mt-4">
            <div className="mobile-task-analysis__label">研判结果</div>
            <div className="mobile-task-analysis__value">{analysis}</div>
          </div>
        )}
        {preservedSecondaryFeedback.map(item => (
          <div key={item.field} className="mobile-task-analysis mt-4">
            <div className="mobile-task-analysis__label">{item.field}记录</div>
            <div className="mobile-task-analysis__value">{item.value}</div>
          </div>
        ))}
      </section>

      {data.qmf_feedback && (
        <Alert
          type="success"
          showIcon
          message="全民防已反馈"
          description={(
            <div className="flex flex-wrap items-center gap-2">
              <span>
                完成时间：{data.qmf_feedback.completed_at
                  ? formatSystemTime(data.qmf_feedback.completed_at)
                  : '已完成，时间待核对'}
              </span>
              <Tag color={QMF_MARKER_STATUS[data.qmf_feedback.tencent_marker_status].color}>
                {QMF_MARKER_STATUS[data.qmf_feedback.tencent_marker_status].label}
              </Tag>
            </div>
          )}
        />
      )}

      {data.photo_requests?.some(request => request.attachments.length > 0) && (
        <section className="app-card mobile-task-photo-results">
          <div>
            <h2 className="font-semibold text-[var(--app-text-strong)]">已调取照片</h2>
            <p className="mt-1 text-xs text-[var(--app-text-secondary)]">
              照片来自已完成的快捷调照片工单，可直接预览或下载原文件。
            </p>
          </div>
          <div className="mobile-task-photo-results__grid">
            {data.photo_requests.flatMap(request => request.attachments.map(attachment => {
              const inlineUrl = workflowApi.attachmentUrl(request.ticket_id, attachment.file_id, true)
              const downloadUrl = workflowApi.attachmentUrl(request.ticket_id, attachment.file_id)
              return (
                <article key={`${request.ticket_id}-${attachment.file_id}`} className="mobile-task-photo-result">
                  <Image
                    className="mobile-task-photo-result__image"
                    src={inlineUrl}
                    alt={attachment.original_name}
                    preview={{ src: inlineUrl }}
                  />
                  <div className="mobile-task-photo-result__meta">
                    <span title={attachment.original_name}>{attachment.original_name}</span>
                    <Button
                      type="link"
                      icon={<DownloadOutlined />}
                      href={downloadUrl}
                    >下载</Button>
                  </div>
                </article>
              )
            }))}
          </div>
        </section>
      )}

      {(data.task.source_count > 1 || data.task.conflict) && (
        <Alert
          type="warning"
          showIcon
          message={`该任务包含 ${data.task.source_count} 条腾讯原始行`}
          description="请先选择具体来源，再分别核对和保存。每次保存只修改当前选中的原始行。"
        />
      )}

      {conflictFields.length > 0 && (
        <section className="app-card p-4">
          <Alert
            type="error"
            showIcon
            message="平台与腾讯表格修改了同一字段"
            description="平台值仍在滨湖平台生效。请逐项核对后决定采用哪一边，系统不会自动覆盖。"
          />
          <div className="mt-4 space-y-3">
            {conflictFields.map(item => (
              <div key={item.field} className="rounded border border-[var(--app-border)] p-3">
                <div className="mb-2 font-medium text-[var(--app-text-strong)]">{item.field}</div>
                <Descriptions
                  size="small"
                  column={mobile ? 1 : 2}
                  items={[
                    { key: 'platform', label: '平台值', children: item.platform_value || '空白' },
                    {
                      key: 'tencent',
                      label: '腾讯值',
                      children: item.error_code === 'source_missing'
                        ? '腾讯来源行已删除或已更换对象'
                        : item.tencent_value || '空白',
                    },
                  ]}
                />
                <div className="mt-3 flex flex-wrap justify-end gap-2">
                  <Button
                    disabled={dirty || item.error_code === 'source_missing'}
                    loading={resolvingConflict === `${item.field}:platform`}
                    onClick={() => void resolveConflict(item.field, 'platform')}
                  >采用平台值</Button>
                  <Button
                    type="primary"
                    danger
                    disabled={dirty}
                    loading={resolvingConflict === `${item.field}:tencent`}
                    onClick={() => void resolveConflict(item.field, 'tencent')}
                  >采用腾讯值</Button>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {data.sources.length > 1 && (
        <section className="app-card mobile-task-source-panel">
          <div>
            <div className="text-sm font-semibold text-[var(--app-text-strong)]">选择腾讯来源</div>
            <p className="mt-1 text-xs text-[var(--app-text-secondary)]">
              {sourceDifferences.length > 0
                ? `以下 ${sourceDifferences.length} 项内容不同，点击卡片切换处理对象`
                : '两条来源的业务内容一致，请按腾讯行号分别处理'}
            </p>
          </div>
          <div className="mobile-task-source-list">
            {data.sources.map((source, index) => (
              <button
                key={source.id}
                type="button"
                className={`mobile-task-source-card${source.id === selectedSourceId ? ' is-selected' : ''}`}
                onClick={() => chooseSource(source)}
              >
                <span className="mobile-task-source-card__header">
                  <span className="font-semibold">来源 {index + 1}</span>
                  <span>{source.source_available ? `腾讯第 ${source.physical_row} 行` : '腾讯来源已删除'}</span>
                  <span className="mobile-task-source-card__state">
                    {source.id === selectedSourceId ? '当前选中' : '点击选择'}
                  </span>
                </span>
                {sourceDifferences.length > 0 && (
                  <span className="mobile-task-source-card__differences">
                    {sourceDifferences.map(difference => (
                      <span key={difference.field} className="mobile-task-source-card__difference">
                        <span>{difference.field}</span>
                        <strong className={!difference.values[index] ? 'is-empty' : ''}>
                          {difference.values[index] || '空白'}
                        </strong>
                      </span>
                    ))}
                  </span>
                )}
              </button>
            ))}
          </div>
        </section>
      )}

      {error && <Alert type="error" showIcon message={error} />}
      {savedMessage && <Alert type="success" showIcon message={savedMessage} />}
      {!data.writeback_enabled && <Alert type="warning" showIcon message="在线回写已暂停，当前任务只能查看" />}

      {selectedSource ? (
        <section className="app-card p-4">
          <div className="mb-4 flex items-center justify-between gap-3">
            <div>
              <h2 className="font-semibold text-[var(--app-text-strong)]">{mode === 'analysis' ? '研判处理' : '核查处理'}</h2>
              <p className="mt-0.5 text-xs text-[var(--app-text-secondary)]">
                {mode === 'analysis' ? '填写或修改研判内容，清空后将重新回到待研判' : '确认所有修改后统一保存'}
              </p>
            </div>
            <span className="text-xs text-[var(--app-text-muted)]">
              {selectedSource.source_available ? `腾讯第 ${selectedSource.physical_row} 行` : '腾讯来源已删除'}
            </span>
          </div>

          {visibleEditorFields.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前任务没有可编辑字段" />
          ) : (
            <div className="space-y-4">
              {visibleEditorFields.map(field => {
                const metadata = selectedSource.cell_meta[field] || { type: 'text' }
                const options = metadata.options?.map(option => ({
                  value: String(option.text),
                  label: String(option.text),
                })) || []
                return (
                  <label key={field} className="block">
                    <span className="mb-1.5 block text-sm font-medium text-[var(--app-text)]">
                      {field === '核查人' ? '任务分配' : field}
                    </span>
                    {field === '核查人' && (
                      <span className="mb-2 block text-xs text-[var(--app-text-secondary)]">选择任务所属社区的在岗组员，保存后即完成转派</span>
                    )}
                    {metadata.type === 'select' || field === '核查人' ? (
                      <Select
                        allowClear
                        showSearch={!mobile}
                        className="w-full"
                        size="large"
                        value={formValues[field] || undefined}
                        options={options}
                        onChange={value => setFormValues(current => ({ ...current, [field]: value || '' }))}
                      />
                    ) : (
                      <Input.TextArea
                        autoSize={{ minRows: field === '现住址' ? 2 : 3, maxRows: 7 }}
                        value={formValues[field] || ''}
                        onChange={event => setFormValues(current => ({ ...current, [field]: event.target.value }))}
                      />
                    )}
                  </label>
                )
              })}
              <Button
                block
                type="primary"
                className="min-h-12"
                icon={<SaveOutlined />}
                loading={saving}
                disabled={!dirty || !data.writeback_enabled}
                onClick={() => void save()}
              >{dirty ? `保存 ${Object.keys(changes).length} 项修改` : '没有未保存修改'}</Button>
            </div>
          )}
        </section>
      ) : <Empty description="没有可用腾讯来源行" />}

      {selectedSource && (
        <Collapse
          items={[{
            key: 'raw',
            label: '查看全部原始字段',
            children: (
              <Descriptions
                size="small"
                column={1}
                bordered
                items={data.workflow.columns
                  .filter(field => selectedSource.values[field])
                  .map(field => ({ key: field, label: field, children: selectedSource.values[field] }))}
              />
            ),
          }]}
        />
      )}

      <Modal
        open={photoRequestOpen}
        title="提交照片调取申请"
        okText="提交工单"
        cancelText="取消"
        confirmLoading={photoSubmitting}
        onOk={() => void submitPhotoRequest()}
        onCancel={() => { if (!photoSubmitting) setPhotoRequestOpen(false) }}
      >
        <div className="space-y-2 text-sm text-[var(--app-text-secondary)]">
          <div><span className="font-medium text-[var(--app-text)]">对象：</span>{title || '未填写姓名'}</div>
          <div><span className="font-medium text-[var(--app-text)]">身份证号：</span>{identityNumber}</div>
          <div><span className="font-medium text-[var(--app-text)]">社区：</span>{selectedSource?.values.社区 || data.task.community || '未填写'}</div>
          <div><span className="font-medium text-[var(--app-text)]">来源：</span>{data.workflow.label || parserType}</div>
        </div>
      </Modal>

      <Modal
        open={qmfPreviewOpen}
        title={qmfPrepareResult
          ? '全民防模型三登记确认'
          : qmfRun
            ? '全民防模型三登记记录'
            : '全民防模型三登记'}
        width={mobile ? 'calc(100vw - 24px)' : 920}
        footer={[
          <Button
            key="close"
            disabled={qmfPreviewLoading || qmfExecuting}
            onClick={() => {
              setQmfPreviewOpen(false)
              setQmfPrepareResult(null)
              setQmfConfirmation('')
              setQmfPreviewError('')
            }}
          >关闭</Button>,
        ]}
        closable={!qmfPreviewLoading && !qmfExecuting}
        maskClosable={!qmfPreviewLoading && !qmfExecuting}
        onCancel={() => {
          if (qmfPreviewLoading || qmfExecuting) return
          setQmfPreviewOpen(false)
          setQmfPrepareResult(null)
          setQmfConfirmation('')
          setQmfPreviewError('')
        }}
      >
        <div className="qmf-preview-modal">
          <Alert
            type="info"
            showIcon
            message={qmfPrepareResult
              ? '登记前核对已完成，确认后将执行全民防登记'
              : qmfRun
                ? '这里只恢复安全步骤状态，不保存或恢复人员照片正文'
                : '正在读取全民防任务、人员资料和居住证照片'}
            description={qmfPrepareResult
              ? '请逐项核对人员、任务、操作人和照片。确认执行后会写入旧平台，提交后不能自动撤销。'
              : '登记前核对不会执行写入；照片只存在于本次认证响应和浏览器内存。'}
          />
          {qmfPreviewLoading && <Skeleton active paragraph={{ rows: 8 }} />}
          {qmfPreviewError && <Alert type="error" showIcon message={qmfPreviewError} />}

          {qmfPrepareResult && (() => {
            const preview = qmfPrepareResult
            return (
              <>
                <section className="qmf-preview-section qmf-preview-person">
                  <div className="qmf-preview-photo">
                    <Image
                      src={`data:${preview.photo.mime_type};base64,${preview.photo.data_base64}`}
                      alt="从居住证获取的照片"
                      preview
                    />
                    <span>{preview.photo.mime_type} · {Math.ceil(preview.photo.size_bytes / 1024)} KB</span>
                  </div>
                  <Descriptions
                    title="人员资料核对"
                    size="small"
                    column={mobile ? 1 : 2}
                    items={[
                      { key: 'name', label: '姓名', children: preview.person.name || '未填写' },
                      { key: 'identity', label: '身份证号', children: preview.person.identity_number || '未填写' },
                      { key: 'phone', label: '手机号', children: preview.person.phone || '未填写' },
                      { key: 'gender', label: '性别', children: preview.person.gender || '未填写' },
                      {
                        key: 'birth',
                        label: '出生日期',
                        children: preview.person.birth_date
                          ? `${preview.person.birth_date}${preview.person.birth_date_derived ? '（由身份证号识别）' : ''}`
                          : '未填写',
                      },
                      { key: 'nation', label: '民族', children: preview.person.nation || '未填写' },
                      { key: 'education', label: '文化程度', children: preview.person.education || '未填写' },
                      { key: 'marriage', label: '婚姻状况', children: preview.person.marital_status || '未填写' },
                      { key: 'person-community-code', label: '人员社区编码', children: preview.person.community_code || '未填写' },
                      { key: 'current-address', label: '现住址', children: preview.person.current_address || '未填写', span: mobile ? 1 : 2 },
                      { key: 'household-address', label: '户籍地址', children: preview.person.household_address || '未填写', span: mobile ? 1 : 2 },
                    ]}
                  />
                </section>

                <section className="qmf-preview-section">
                  <Descriptions
                    title="全民防待处理任务"
                    size="small"
                    column={mobile ? 1 : 2}
                    items={[
                      { key: 'station', label: '派出所', children: preview.upstream_task.police_station || '未填写' },
                      { key: 'community', label: '社区', children: preview.upstream_task.community || '未填写' },
                      { key: 'task-community-code', label: '任务辖区编码', children: preview.upstream_task.community_code || '未填写' },
                      { key: 'status', label: '上游状态', children: preview.upstream_task.check_status_text || preview.upstream_task.check_status || '未填写' },
                      { key: 'dispatch', label: '下发时间', children: preview.upstream_task.dispatch_time || '未填写' },
                      { key: 'address', label: '任务地址', children: preview.upstream_task.address || '未填写', span: mobile ? 1 : 2 },
                    ]}
                  />
                </section>

                <section className="qmf-preview-section">
                  <h3>安全校验</h3>
                  <div className="qmf-preview-disabled-steps">
                    {[
                      ['source_revision', '来源版本一致'],
                      ['single_source', '平台来源唯一'],
                      ['identity_match', '身份证一致'],
                      ['name_match', '姓名一致'],
                      ['single_upstream_task', '上游任务唯一'],
                      ['station_match', '派出所一致'],
                      ['person_match', '人员资料一致'],
                      ['jurisdiction_match', '辖区按派出所校验'],
                      ['precheck_passed', '登记前校验通过'],
                      ['photo_valid', '照片格式有效'],
                    ].map(([key, label]) => (
                      <Tag key={key} color={preview.checks[key] ? 'success' : 'error'}>{label}</Tag>
                    ))}
                  </div>
                </section>

                <section className="qmf-preview-section">
                  <Descriptions
                    title="当前全民防登录身份"
                    size="small"
                    column={mobile ? 1 : 2}
                    items={[
                      { key: 'operator', label: '操作人', children: preview.operator.name },
                      { key: 'operator-id', label: '操作账号', children: preview.operator.username },
                      { key: 'operator-station', label: '所属机构', children: preview.operator.station_name },
                      { key: 'operator-station-code', label: '机构代码', children: preview.operator.station_code },
                    ]}
                  />
                </section>

                <section className="qmf-preview-section">
                  <h3>本次固定执行顺序</h3>
                  <div className="qmf-preview-disabled-steps">
                    {preview.planned_write_steps.map(step => (
                      <span key={step.key}>
                        {step.label}<Tag color="processing">将执行</Tag>
                      </span>
                    ))}
                  </div>
                </section>

                <section className="qmf-preview-section">
                  <h3>预计字段变化</h3>
                  <div className="qmf-registration-changes">
                    {preview.planned_changes.map(change => (
                      <div key={change.key} className="qmf-registration-change">
                        <strong>{change.label}</strong>
                        <span>{change.detail}</span>
                      </div>
                    ))}
                  </div>
                </section>
              </>
            )
          })()}

          {qmfRun && (
            <section className="qmf-preview-section qmf-registration-run">
              <div className="qmf-registration-run__header">
                <h3>九步执行状态</h3>
                <Tag color={QMF_RUN_STATUS[qmfRun.status].color}>
                  {QMF_RUN_STATUS[qmfRun.status].label}
                </Tag>
              </div>
              <div className="qmf-registration-steps">
                {qmfRun.steps.map((step, index) => (
                  <div key={step.key} className={`qmf-registration-step is-${step.status}`}>
                    <span className="qmf-registration-step__index">{index + 1}</span>
                    <span className="qmf-registration-step__label">{step.label}</span>
                    <Tag color={QMF_STEP_STATUS[step.status].color}>
                      {QMF_STEP_STATUS[step.status].label}
                    </Tag>
                  </div>
                ))}
              </div>
              {qmfRun.completed_at && (
                <p className="text-xs text-[var(--app-text-secondary)]">
                  全民防完成时间：{formatSystemTime(qmfRun.completed_at)}
                </p>
              )}
              {qmfRun.status === 'uncertain' && (
                <Alert type="error" showIcon message="外部结果无法确认，本条已冻结" description="请先到全民防旧平台人工核对；系统不会自动重试，也不能从头重放。" />
              )}
              {qmfRun.status === 'failed' && (
                <div className="space-y-3">
                  <Alert
                    type="error"
                    showIcon
                    message="登记已停止"
                    description={canReprepareQmfRun
                      ? '系统确认尚未开始任何写入。你可以人工重新核对任务、人员和照片后，再生成一次新的登记准备。'
                      : '本条存在写入进度或无法安全排除外部影响，仍保持冻结，不能从头重放。'}
                  />
                  {canReprepareQmfRun && (
                    <Button type="primary" onClick={() => void openQmfRegistration()}>
                      重新核对并准备
                    </Button>
                  )}
                </div>
              )}
              {qmfRun.status === 'succeeded' && (
                <div className="flex flex-wrap items-center gap-2">
                  <Tag color={QMF_MARKER_STATUS[qmfRun.tencent_marker_status].color}>
                    {QMF_MARKER_STATUS[qmfRun.tencent_marker_status].label}
                  </Tag>
                  {qmfRun.can_retry_marker && (
                    <Button loading={qmfMarkerRetrying} onClick={() => void retryQmfMarker()}>
                      仅重试腾讯完成标记
                    </Button>
                  )}
                </div>
              )}
            </section>
          )}

          {qmfRun?.status === 'prepared' && qmfPrepareResult && (
            <section className="qmf-preview-section qmf-registration-confirm">
              <Alert
                type="warning"
                showIcon
                message="执行前请再次核对"
                description="执行前还会再次读取腾讯来源和全民防任务；任一内容变化都会在写入前停止。"
              />
              <Button
                block
                type="primary"
                loading={qmfExecuting}
                disabled={!canExecutePreparedQmfRun(qmfRun, true)}
                onClick={executePreparedQmfRun}
              >二次确认并执行全民防登记</Button>
            </section>
          )}

          {qmfRun?.status === 'prepared' && !qmfPrepareResult && (
            <section className="qmf-preview-section">
              <Alert type="warning" showIcon message="准备资料未保存在平台" description="为保证你重新看到完整人员资料和照片，执行前必须再次完成登记前核对。" />
              <Button type="primary" onClick={() => void openQmfRegistration()}>
                重新核对并准备
              </Button>
            </section>
          )}
        </div>
      </Modal>
    </div>
  )
}
