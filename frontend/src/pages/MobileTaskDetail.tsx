import {
  ArrowLeftOutlined,
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
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  getMobileTaskDetail,
  getMobileTaskAnalysisDetail,
  getMobileTaskResidenceDetail,
  manuallyConfirmRegistration,
  getQmfLegacyStatus,
  getQmfRegistrationRun,
  executeQmfRegistration,
  prepareQmfRegistration,
  retryQmfTencentMarker,
  searchRegistrationProperties,
  resolveMobileTaskSyncConflict,
  updateMobileTask,
  updateMobileTaskAnalysis,
  decideMobileTaskUnverifiableReview,
  workflowApi,
  type MobileTaskDetailData,
  type MobileTaskQmfStatus,
  type MobileTaskSource,
  type QmfLegacyStatus,
  type QmfPrepareResult,
  type QmfRegistrationRun,
  type ResidenceRegistrationDetail as ResidenceDetail,
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
  mobileTaskCurrentAddressLabel,
  mobileTaskEditorFields,
  mobileTaskPhoneOptions,
  mobileTaskResultOptions,
  mobileTaskSourceTags,
  mobileTaskSourceDifferences,
  mobileTaskSourceNeedsReview,
  mobileTaskSourceState,
  mobileTaskUsesRegistrationClosure,
} from '../utils/mobileTasks'
import MobilePhonePicker from '../components/MobilePhonePicker'
import QmfFeedbackStatus from '../components/QmfFeedbackStatus'
import ResidenceRegistrationStatus from '../components/ResidenceRegistrationStatus'
import ResidenceRegistrationDetail from '../components/ResidenceRegistrationDetail'
import RegistrationLinkStatus from '../components/RegistrationLinkStatus'
import useMobileViewport from '../hooks/useMobileViewport'
import useSystemTime from '../hooks/useSystemTime'
import { openNativePhoneDialer } from '../utils/nativePhone'
import {
  QMF_MARKER_STATUS,
  QMF_RUN_STATUS,
  QMF_STEP_STATUS,
  canExecutePreparedQmfRun,
  qmfLegacyStatusAllowsRegistration,
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

const STRUCTURED_REVIEW_TYPES = new Set([
  '全链条', '出租房屋核查', '寄递业', '疑似返苏', '苏州涉警', '交通涉警',
])

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

function realtimeQmfSnapshot(
  result: QmfLegacyStatus,
  platformResult: string,
  previous: MobileTaskQmfStatus | null | undefined,
): MobileTaskQmfStatus | null {
  const stateMap: Partial<Record<QmfLegacyStatus['state'], MobileTaskQmfStatus['state']>> = {
    pending: 'pending',
    completed_match: 'completed_match',
    completed_mismatch: 'completed_mismatch',
    not_found: 'not_found',
    non_jurisdiction: 'non_jurisdiction',
  }
  const state = stateMap[result.state]
  if (!state) return null
  return {
    state,
    platform_result: platformResult,
    feedback_result: result.result || result.result_text || '',
    checked_at: result.checked_at || '',
    origin: ['completed_match', 'completed_mismatch'].includes(result.state)
      ? (result.origin || previous?.origin || '')
      : '',
    error_code: '',
    last_scanned_at: new Date().toISOString(),
  }
}

export default function MobileTaskDetail({ mode = 'tasks' }: { mode?: 'tasks' | 'analysis' }) {
  const navigate = useNavigate()
  const mobile = useMobileViewport()
  const formatSystemTime = useSystemTime()
  const { recordActivity, user } = useAuth()
  const { parserType = '', rowKey = '' } = useParams()
  const registrationClosureEnabled = mobileTaskUsesRegistrationClosure(parserType)
  const [searchParams] = useSearchParams()
  const readonlyView = searchParams.get('readonly') === '1'
  const [data, setData] = useState<MobileTaskDetailData | null>(null)
  const [selectedSourceId, setSelectedSourceId] = useState<number | null>(null)
  const [formValues, setFormValues] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [resolvingConflict, setResolvingConflict] = useState('')
  const [error, setError] = useState('')
  const [savedMessage, setSavedMessage] = useState('')
  const [decisionOutcome, setDecisionOutcome] = useState<'success' | 'failure'>('success')
  const [decisionOpinion, setDecisionOpinion] = useState('')
  const [photoRequestOpen, setPhotoRequestOpen] = useState(false)
  const [photoSubmitting, setPhotoSubmitting] = useState(false)
  const [qmfPreviewOpen, setQmfPreviewOpen] = useState(false)
  const [qmfPreviewLoading, setQmfPreviewLoading] = useState(false)
  const [qmfPrepareResult, setQmfPrepareResult] = useState<QmfPrepareResult | null>(null)
  const [qmfRun, setQmfRun] = useState<QmfRegistrationRun | null>(null)
  const [qmfExecuting, setQmfExecuting] = useState(false)
  const [qmfMarkerRetrying, setQmfMarkerRetrying] = useState(false)
  const [qmfPreviewError, setQmfPreviewError] = useState('')
  const [qmfLegacyStatus, setQmfLegacyStatus] = useState<QmfLegacyStatus | null>(null)
  const [qmfLegacyStatusLoading, setQmfLegacyStatusLoading] = useState(false)
  const [qmfLegacyStatusError, setQmfLegacyStatusError] = useState('')
  const [residenceDetail, setResidenceDetail] = useState<ResidenceDetail | null>(null)
  const [residenceDetailLoading, setResidenceDetailLoading] = useState(false)
  const [residenceDetailError, setResidenceDetailError] = useState('')
  const [registrationProperties, setRegistrationProperties] = useState<Array<{
    id: number
    natural_address: string
    building: string
    room: string
    version: number
    community_name: string
  }>>([])
  const [registrationPropertyId, setRegistrationPropertyId] = useState<number | undefined>()
  const [registrationPropertyVersion, setRegistrationPropertyVersion] = useState<number | undefined>()
  const [registrationPropertyLoading, setRegistrationPropertyLoading] = useState(false)
  const [manualConfirmOpen, setManualConfirmOpen] = useState(false)
  const [manualConfirmReason, setManualConfirmReason] = useState<'address_mismatch' | 'address_ambiguous'>('address_mismatch')
  const [manualConfirmNote, setManualConfirmNote] = useState('')
  const [manualConfirming, setManualConfirming] = useState(false)
  const qmfPreviewRequestActive = useRef(false)

  const selectedSource = useMemo(
    () => data?.sources.find(source => source.id === selectedSourceId) || null,
    [data, selectedSourceId],
  )
  const interactionLocked = readonlyView
  const visibleEditorFields = useMemo(() => (
    !interactionLocked && data && selectedSource
      ? mobileTaskEditorFields(
          data,
          selectedSource.editable_fields,
          formValues,
          selectedSource.values,
        )
      : []
  ), [data, formValues, interactionLocked, selectedSource])
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

  useEffect(() => {
    if (!data || !selectedSource || mode !== 'analysis' || !STRUCTURED_REVIEW_TYPES.has(parserType)) return
    const flow = data.task.review_flow
    if (flow && (flow.state === 'initial_pending' || flow.state === 'deep_pending')) {
      // 每一层研判都必须重新填写本阶段意见，不能把上一阶段意见当作默认值。
      setDecisionOpinion('')
    } else {
      setDecisionOpinion('')
    }
  }, [data, mode, parserType, selectedSource])

  const load = useCallback(async (preferredSourceId?: number) => {
    setLoading(true)
    setError('')
    try {
      const result = mode === 'analysis'
        ? await getMobileTaskAnalysisDetail(parserType, rowKey)
        : await getMobileTaskDetail(parserType, rowKey)
      setData(result)
      const link = result.registration_link || result.task.registration_link
      const source = result.sources.find(item => item.id === preferredSourceId) || result.sources[0]
      const pendingRegistration = Boolean(
        source
        && mobileTaskUsesRegistrationClosure(parserType)
        && (source.values[result.workflow.result_field] || '').trim() === '待登记',
      )
      setRegistrationPropertyId(pendingRegistration ? link?.property_id || undefined : undefined)
      setRegistrationPropertyVersion(pendingRegistration ? link?.property_version || undefined : undefined)
      setRegistrationProperties(pendingRegistration && link?.property ? [link.property] : [])
      if (source) selectSource(source)
    } catch (reason: any) {
      setError(detailError(reason, '任务详情读取失败'))
    } finally {
      setLoading(false)
    }
  }, [mode, parserType, rowKey, selectSource])

  useEffect(() => { void load() }, [load])

  useEffect(() => {
    if (data?.task.residence_status?.state !== 'registered') {
      setResidenceDetail(null)
      setResidenceDetailError('')
      setResidenceDetailLoading(false)
      return
    }
    let cancelled = false
    setResidenceDetail(null)
    setResidenceDetailError('')
    setResidenceDetailLoading(true)
    void getMobileTaskResidenceDetail(parserType, rowKey).then(result => {
      if (!cancelled) setResidenceDetail(result)
    }).catch(reason => {
      if (!cancelled) {
        setResidenceDetailError(detailError(reason, '居住证人员资料暂时无法读取'))
      }
    }).finally(() => {
      if (!cancelled) setResidenceDetailLoading(false)
    })
    return () => { cancelled = true }
  }, [data?.task.residence_status?.state, parserType, rowKey])

  useEffect(() => {
    if (
      interactionLocked
      || mode !== 'tasks'
      || !data?.qmf_registration?.visible
      || !selectedSource?.source_available
    ) {
      setQmfLegacyStatus(null)
      setQmfLegacyStatusError('')
      setQmfLegacyStatusLoading(false)
      return
    }
    let cancelled = false
    setQmfLegacyStatus(null)
    setQmfLegacyStatusError('')
    setQmfLegacyStatusLoading(true)
    void getQmfLegacyStatus({
      parser_type: parserType,
      row_key: rowKey,
      source_id: selectedSource.id,
      expected_revision: selectedSource.revision,
    }).then(result => {
      if (!cancelled) {
        setQmfLegacyStatus(result)
        const snapshot = realtimeQmfSnapshot(
          result,
          data?.task.summary.result || '',
          data?.task.qmf_status,
        )
        if (snapshot) {
          setData(current => current ? {
            ...current,
            task: { ...current.task, qmf_status: snapshot },
            qmf_status: snapshot,
          } : current)
        }
      }
    }).catch(reason => {
      if (!cancelled) {
        setQmfLegacyStatusError(detailError(reason, '全民防反馈状态暂时无法确认'))
      }
    }).finally(() => {
      if (!cancelled) setQmfLegacyStatusLoading(false)
    })
    return () => { cancelled = true }
  }, [data?.qmf_registration?.visible, interactionLocked, mode, parserType, rowKey, selectedSource?.id, selectedSource?.revision, selectedSource?.source_available])

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
    if (interactionLocked || !selectedSource || !dirty) return
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
        ...(registrationPropertyId && registrationPropertyVersion
          ? {
              registration_property_id: registrationPropertyId,
              registration_property_version: registrationPropertyVersion,
            }
          : {}),
      })
      const savedValues = mergeMobileTaskSaveValues(
        selectedSource.values,
        changes,
        result.values,
        selectedSource.cell_meta,
      )
      const savedResult = savedValues[data.workflow.result_field] || ''
      const savedAnalysis = firstValue(savedValues, data.workflow.analysis_fields)
      const savedReviewStage = savedResult.includes('无法核实')
        ? (savedAnalysis ? 'analyzed' : 'waiting_analysis')
        : ''
      const dependencyStillPending = savedReviewStage === 'waiting_analysis'
      setData(current => current ? {
        ...current,
        dependency_blocked: mode === 'tasks'
          ? dependencyStillPending
          : current.dependency_blocked,
        dependency_message: mode === 'tasks' && !dependencyStillPending
          ? ''
          : current.dependency_message,
        task: {
          ...current.task,
          pending_sync: true,
          sync_state: result.sync_state,
          review_stage: savedReviewStage,
          summary: {
            ...current.task.summary,
            result: savedResult,
            analysis: savedAnalysis,
          },
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
          review_stage: savedReviewStage,
        } : source),
      } : current)
      setFormValues(savedValues)
      // 保存“待登记”后，服务端会同时创建/更新房屋关联。重新读取一次详情，
      // 确保比对阶段、房屋版本和 registration_link 与服务端一致。
      await load(selectedSource.id)
      setSavedMessage(result.message)
    } catch (reason: any) {
      const status = reason?.response?.status
      setError(detailError(reason, '保存失败，请稍后重试'))
      if (status === 409 || status === 502) await load(selectedSource.id)
    } finally {
      setSaving(false)
    }
  }

  const submitStructuredDecision = async () => {
    if (
      interactionLocked
      || !selectedSource
      || !data
      || !STRUCTURED_REVIEW_TYPES.has(parserType)
    ) return
    const flow = data.task.review_flow
    if (!flow || !['initial_pending', 'deep_pending'].includes(flow.state)) {
      setError('当前处于延时复核阶段，请等待系统到期后再处理')
      return
    }
    if (!decisionOpinion.trim()) {
      setError('请填写研判意见')
      return
    }
    setSaving(true)
    setError('')
    setSavedMessage('')
    try {
      const result = await decideMobileTaskUnverifiableReview(parserType, selectedSource.id, {
        stage: flow.state as 'initial_pending' | 'deep_pending',
        outcome: decisionOutcome,
        opinion: decisionOpinion.trim(),
        flow_version: flow.flow_version,
        expected_revision: selectedSource.revision,
        expected_row_hash: selectedSource.row_hash,
      })
      setSavedMessage(result.message)
      await load(selectedSource.id)
    } catch (reason: any) {
      setError(detailError(reason, '研判决定提交失败，请刷新后重试'))
      if (reason?.response?.status === 409) await load(selectedSource.id)
    } finally {
      setSaving(false)
    }
  }

  const loadRegistrationProperties = async (keyword: string) => {
    if (!data || !keyword.trim()) {
      setRegistrationProperties([])
      return
    }
    setRegistrationPropertyLoading(true)
    try {
      const result = await searchRegistrationProperties(keyword, data.task.community)
      setRegistrationProperties(result.data || [])
    } catch {
      setRegistrationProperties([])
    } finally {
      setRegistrationPropertyLoading(false)
    }
  }

  const openManualRegistrationConfirm = () => {
    if (!registrationLink || !selectedSource) return
    setManualConfirmReason(
      registrationLink.reason_code === 'address_ambiguous'
        ? 'address_ambiguous'
        : 'address_mismatch',
    )
    setManualConfirmNote('')
    setManualConfirmOpen(true)
  }

  const confirmManualRegistration = async () => {
    if (!selectedSource || manualConfirming) return
    setManualConfirming(true)
    setError('')
    try {
      const result = await manuallyConfirmRegistration(parserType, rowKey, {
        reason: manualConfirmReason,
        note: manualConfirmNote.trim(),
        expected_revision: selectedSource.revision,
      })
      message.success(result.message || '已提交登记复核')
      setManualConfirmOpen(false)
      await load(selectedSource.id)
    } catch (reason: any) {
      setError(detailError(reason, '人工确认登记失败'))
    } finally {
      setManualConfirming(false)
    }
  }

  const resolveConflict = async (field: string, choice: 'platform' | 'tencent') => {
    if (interactionLocked || !selectedSource || dirty) return
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
    try {
      if (await openNativePhoneDialer(phone)) return
    } catch {
      await navigator.clipboard.writeText(phone).catch(() => {})
      message.error('无法打开系统拨号界面，电话号码已复制')
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
  const registrationPending = registrationClosureEnabled
    && selectedSource
    && (selectedSource.values[data.workflow.result_field] || '').trim() === '待登记'
  const currentAddressLabel = mobileTaskCurrentAddressLabel(
    parserType,
    registrationPending ? '待登记' : selectedSource?.values[data.workflow.result_field] || '',
  )
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
      ? [{ label: currentAddressLabel, value: currentAddress, wide: true, copyValue: currentAddress, copyLabel: currentAddressLabel }]
      : []),
    ...(!originalAddress && !currentAddress
      ? [{ label: '地址', value: '未填写', wide: true }]
      : []),
  ]
  const latestQmfRun = qmfRun || data.qmf_registration?.latest_run || null
  const registrationLink = data.registration_link || data.task.registration_link || null
  const reviewFlow = data.task.review_flow || null
  const canReprepareQmfRun = qmfRunCanReprepare(qmfRun)
  const shouldResumeQmfRun = Boolean(
    latestQmfRun
    && ['prepared', 'executing', 'succeeded', 'failed', 'uncertain'].includes(latestQmfRun.status),
  )
  const qmfStatusAllowsRegistration = qmfLegacyStatusAllowsRegistration(qmfLegacyStatus)
  const qmfLegacyStatusView = qmfLegacyStatus ? (() => {
    switch (qmfLegacyStatus.state) {
      case 'pending':
        return { type: 'info' as const, message: '全民防尚未反馈', description: '可以继续生成全民防登记准备；执行前还会再次复核。' }
      case 'not_found':
        return { type: 'info' as const, message: '管理端未查到该记录', description: '这不等于未反馈；登记准备会继续通过手机待办接口确认唯一任务。' }
      case 'non_jurisdiction':
        return {
          type: 'warning' as const,
          message: '全民防返回非本辖区，请重新提交结果',
          description: `${qmfLegacyStatus.result_text || '非本辖区（无法提交）'}${qmfLegacyStatus.checked_at ? ` · ${qmfLegacyStatus.checked_at}` : ''}；准备后将只新增一次特殊反馈。`,
        }
      case 'completed_match':
        return {
          type: 'success' as const,
          message: '全民防已反馈，无需重复登记',
          description: `${qmfLegacyStatus.result_text || '结果已确认'}${qmfLegacyStatus.checked_at ? ` · ${qmfLegacyStatus.checked_at}` : ''} · ${qmfLegacyStatus.origin === 'binhu_automatic' ? '由滨湖平台完成' : 'APP 手工或其他渠道完成'}`,
        }
      case 'completed_mismatch':
        return { type: 'error' as const, message: '全民防反馈结果与平台核查结果不一致', description: `${qmfLegacyStatus.result_text || '结果待核对'}${qmfLegacyStatus.checked_at ? ` · ${qmfLegacyStatus.checked_at}` : ''}，请先人工核对。` }
      case 'ambiguous':
        return { type: 'warning' as const, message: '全民防存在多条匹配记录', description: '为避免误登记，当前不能继续。' }
      case 'station_mismatch':
        return { type: 'warning' as const, message: '全民防记录不属于滨湖新城派出所', description: qmfLegacyStatus.station || '请人工核对记录归属。' }
      case 'unknown_result':
        return { type: 'warning' as const, message: '全民防核查结果暂不支持', description: '请人工核对全民防记录后再处理。' }
      default:
        return { type: 'warning' as const, message: '全民防反馈状态暂时无法确认', description: qmfLegacyStatus.reason || '为避免重复登记，当前不能继续。' }
    }
  })() : null

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
        {parserType === '全链条' && sourceTags.length > 0 && (
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
          {!interactionLocked && <MobilePhonePicker
              phones={phoneOptions}
              mode="dial"
              label={phoneOptions.length > 1 ? `选择拨打（${phoneOptions.length}）` : '拨打电话'}
              buttonProps={{ className: 'mobile-task-detail-pill', type: 'primary', icon: <PhoneOutlined /> }}
              onSelect={value => void dial(value)}
            />}
          {interactionLocked && phoneOptions.length > 0 && <Button disabled className="mobile-task-detail-pill" icon={<PhoneOutlined />}>只读模式</Button>}
          {phoneOptions.length === 0 && (
            <Button disabled className="mobile-task-detail-pill" icon={<PhoneOutlined />}>缺少电话号码</Button>
          )}
          {!interactionLocked && mode === 'tasks' && data.qmf_registration?.visible && (
            <Button
              type="primary"
              className="mobile-task-detail-pill"
              icon={<SafetyCertificateOutlined />}
              disabled={
                (!shouldResumeQmfRun && (
                  !data.qmf_registration.enabled
                  || qmfLegacyStatusLoading
                  || !qmfStatusAllowsRegistration
                ))
                || !selectedSource?.source_available
                || dirty
                || qmfPreviewLoading
              }
              title={dirty
                ? '请先保存或放弃当前修改'
                : !selectedSource?.source_available
                  ? '腾讯来源行已不存在，不能准备登记'
                  : qmfLegacyStatusLoading
                    ? '正在复核全民防反馈状态'
                    : qmfLegacyStatusError
                      ? qmfLegacyStatusError
                      : qmfLegacyStatus?.reason || data.qmf_registration.reason}
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

      {mode === 'tasks' && reviewFlow && !['resolved', 'archived'].includes(reviewFlow.state) && (
        <Alert
          type={reviewFlow.state === 'source_exception' || reviewFlow.state === 'final_unverifiable' ? 'warning' : 'info'}
          showIcon
          message={reviewFlow.state_label}
          description={(
            <div className="grid gap-2">
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                {reviewFlow.review_due_date && <span>复核截止：{reviewFlow.review_due_date}</span>}
                {['initial_extension', 'deep_extension'].includes(reviewFlow.state) && (
                  <span>本轮反馈：{reviewFlow.feedback_submitted ? '已记录' : '未记录'}</span>
                )}
              </div>
              {reviewFlow.state === 'source_exception' ? (
                <strong>来源信息发生变化，自动流转已经暂停，请联系基础管控复核。</strong>
              ) : reviewFlow.state === 'final_unverifiable' ? (
                <strong>该任务已形成最终无法核实，等待在当前业务的归档 Panel 中导出。</strong>
              ) : (
                <strong>核查对象一旦已经能够核实，请立即修改“核查结果”；不要只填写二次反馈，否则任务仍会按无法核实流程继续流转。</strong>
              )}
            </div>
          )}
        />
      )}

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

      {data.task.qmf_status && (
        <section className="app-card flex flex-wrap items-center justify-between gap-3 p-4">
          <div>
            <h2 className="font-semibold text-[var(--app-text-strong)]">全民防反馈核对</h2>
            <p className="mt-1 text-xs text-[var(--app-text-secondary)]">
              这是后台只读扫描的最近缓存结果；执行全民防登记前仍会重新实时核对。
            </p>
          </div>
          <QmfFeedbackStatus status={data.task.qmf_status} />
        </section>
      )}

      {data.task.residence_status && (
        <section className="app-card p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="font-semibold text-[var(--app-text-strong)]">居住证登记核对</h2>
              <p className="mt-1 text-xs text-[var(--app-text-secondary)]">
                后台按身份证号只读查询“人员登记、注销”；已有登记资料时在此实时读取详情。
              </p>
            </div>
            <ResidenceRegistrationStatus status={data.task.residence_status} />
          </div>
          {data.task.residence_status.state === 'registered' && (
            <div className="mt-5 border-t border-[var(--app-border)] pt-5">
              <ResidenceRegistrationDetail
                detail={residenceDetail}
                loading={residenceDetailLoading}
                error={residenceDetailError}
              />
            </div>
          )}
        </section>
      )}

      {registrationClosureEnabled && registrationLink && (
        <section className="app-card p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="font-semibold text-[var(--app-text-strong)]">待登记房屋关联</h2>
              <p className="mt-1 text-xs text-[var(--app-text-secondary)]">
                仅在居住证连续两个独立扫描周期精确匹配同一套有效房屋后，才会自动转为已登记。
              </p>
            </div>
            <RegistrationLinkStatus link={registrationLink} />
          </div>
          {registrationLink.property ? (
            <Descriptions
              className="mt-4"
              size="small"
              column={1}
              bordered
              items={[
                {
                  key: 'property',
                  label: '关联房屋',
                  children: [
                    registrationLink.property.community_name,
                    registrationLink.property.natural_address,
                    registrationLink.property.building,
                    registrationLink.property.room,
                  ].filter(Boolean).join(' '),
                },
                {
                  key: 'match',
                  label: '比对进度',
                  children: `${registrationLink.match_count || 0} 次连续匹配${registrationLink.confirmed_at ? ` · ${registrationLink.confirmed_at}` : ''}`,
                },
                ...(registrationLink.reason ? [{
                  key: 'reason',
                  label: '当前说明',
                  children: registrationLink.reason,
                }] : []),
              ]}
            />
          ) : (
            <Alert className="mt-4" type="warning" showIcon message="尚未关联辖区档案房屋" description="选择“待登记”时必须从当前任务社区的房屋档案中明确选择一套房屋。" />
          )}
          {data.registration_manual_confirm_allowed
            && registrationLink.status === 'review_required'
            && ['address_mismatch', 'address_ambiguous'].includes(registrationLink.reason_code)
            && selectedSource && (
            <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-[var(--app-border)] bg-[var(--app-surface-muted)] p-3">
              <div className="text-xs text-[var(--app-text-secondary)]">
                居住证已有有效登记，但自动地址匹配需要人工复核；确认后仍需后台写回腾讯表格。
              </div>
              <Button type="primary" onClick={openManualRegistrationConfirm}>
                人工确认已登记
              </Button>
            </div>
          )}
        </section>
      )}

      {mode === 'tasks' && data.qmf_registration?.visible && (
        <div className="space-y-2">
          {qmfLegacyStatusLoading && <Alert type="info" showIcon message="正在复核全民防反馈状态" />}
          {qmfLegacyStatusError && <Alert type="warning" showIcon message="全民防反馈状态暂时无法确认" description={qmfLegacyStatusError} />}
          {qmfLegacyStatusView && (
            <Alert
              type={qmfLegacyStatusView.type}
              showIcon
              message={qmfLegacyStatusView.message}
              description={qmfLegacyStatusView.description}
            />
          )}
        </div>
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
                    disabled={interactionLocked || dirty || item.error_code === 'source_missing'}
                    loading={resolvingConflict === `${item.field}:platform`}
                    onClick={() => void resolveConflict(item.field, 'platform')}
                  >采用平台值</Button>
                  <Button
                    type="primary"
                    danger
                    disabled={interactionLocked || dirty}
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
      {readonlyView && <Alert type="info" showIcon message="当前是任务图只读协作视图" description="你可以查看任务信息和协作结果，但不能在此修改字段、处理同步冲突或发起新的业务操作。" />}
      {data.dependency_blocked && <Alert type="warning" showIcon message="该任务已进入研判队列" description={data.dependency_message || '网格员仍可继续核查；如已能核实，请直接修改并保存新的核查结果。'} />}
      {!data.writeback_enabled && <Alert type="warning" showIcon message="在线回写已暂停，当前任务只能查看" />}

      {selectedSource ? (
        <section className="app-card p-4">
          <div className="mb-4 flex items-center justify-between gap-3">
            <div>
              <h2 className="font-semibold text-[var(--app-text-strong)]">{readonlyView ? '任务只读详情' : mode === 'analysis' ? '研判处理' : '核查处理'}</h2>
              <p className="mt-0.5 text-xs text-[var(--app-text-secondary)]">
                {readonlyView
                  ? '该任务属于依赖链中的其他负责人，仅供了解前置或后置关系'
                  : mode === 'analysis'
                    ? STRUCTURED_REVIEW_TYPES.has(parserType)
                      ? '按当前阶段选择研判成功或失败，并填写本阶段意见'
                      : '填写或修改研判内容，清空后将重新回到待研判'
                    : data.dependency_blocked
                      ? '基础管控可同时研判；重新核实后可直接修改结果并保存'
                      : '确认所有修改后统一保存'}
              </p>
            </div>
            <span className="text-xs text-[var(--app-text-muted)]">
              {selectedSource.source_available ? `腾讯第 ${selectedSource.physical_row} 行` : '腾讯来源已删除'}
            </span>
          </div>

          {mode === 'analysis' && STRUCTURED_REVIEW_TYPES.has(parserType) ? (
            <div className="space-y-4">
              <Alert
                type="info"
                showIcon
                message={data.task.review_flow?.state_label || '两级研判'}
                description={data.task.review_flow?.review_due_date
                  ? `系统计算的复核截止日期：${data.task.review_flow.review_due_date}，到期后自动进入下一阶段。`
                  : '请明确选择研判成功或研判失败，并填写结构化意见。'}
              />
              {data.task.review_flow && ['initial_pending', 'deep_pending'].includes(data.task.review_flow.state) ? (
                <>
                  <Select
                    className="w-full"
                    size="large"
                    value={decisionOutcome}
                    options={[
                      { value: 'success', label: '研判成功（进入延时复核）' },
                      { value: 'failure', label: '研判失败（进入下一阶段）' },
                    ]}
                    onChange={value => setDecisionOutcome(value)}
                  />
                  <Input.TextArea
                    rows={5}
                    maxLength={2000}
                    showCount
                    value={decisionOpinion}
                    placeholder="请填写本阶段研判意见（必填）"
                    onChange={event => setDecisionOpinion(event.target.value)}
                  />
                  <Button
                    type="primary"
                    icon={<SaveOutlined />}
                    loading={saving}
                    disabled={!selectedSource.source_available || !decisionOpinion.trim()}
                    onClick={() => void submitStructuredDecision()}
                  >提交本阶段研判</Button>
                </>
              ) : (
                <Alert type="warning" showIcon message="当前正在延时复核，暂不能重复提交研判决定" />
              )}
            </div>
          ) : visibleEditorFields.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前任务没有可编辑字段" />
          ) : (
            <div className="space-y-4">
              {visibleEditorFields.map(field => {
                const metadata = selectedSource.cell_meta[field] || { type: 'text' }
                const resultField = field === data.workflow.result_field
                const optionSource = resultField
                  ? mobileTaskResultOptions(
                    metadata.options,
                    registrationClosureEnabled,
                  )
                  : metadata.options || []
                const options = optionSource.map(option => ({
                  value: String(option.text),
                  label: String(option.text),
                }))
                return (
                  <label
                    key={field}
                    className={`block${field === '核查反馈' ? ' mobile-task-detail-editor-field--compact' : ''}`}
                  >
                    <span className="mb-1.5 block text-sm font-medium text-[var(--app-text)]">
                      {field === '核查人'
                        ? '任务分配'
                        : registrationClosureEnabled && field === '现住址'
                          ? mobileTaskCurrentAddressLabel(
                            parserType,
                            formValues[data.workflow.result_field] || '',
                          )
                          : field}
                    </span>
                    {field === '核查人' && (
                      <span className="mb-2 block text-xs text-[var(--app-text-secondary)]">选择任务所属社区的在岗组员，保存后即完成转派</span>
                    )}
                    {registrationClosureEnabled && field === '现住址'
                      && data.workflow.result_field
                      && (formValues[data.workflow.result_field] || '').trim() === '待登记' ? (
                      <Select
                        showSearch
                        allowClear
                        filterOption={false}
                        className="w-full"
                        size="large"
                        loading={registrationPropertyLoading}
                        value={registrationPropertyId}
                        placeholder="搜索并选择辖区档案中的唯一房屋"
                        options={registrationProperties.map(property => ({
                          value: property.id,
                          label: `${property.natural_address || ''}${property.building || ''}${property.room || ''}`.trim(),
                        }))}
                        onSearch={value => void loadRegistrationProperties(value)}
                        onChange={value => {
                          const property = registrationProperties.find(item => item.id === value)
                          setRegistrationPropertyId(value || undefined)
                          setRegistrationPropertyVersion(property?.version)
                          if (property) {
                            setFormValues(current => ({
                              ...current,
                              现住址: `${property.natural_address || ''}${property.building || ''}${property.room || ''}`,
                            }))
                          }
                        }}
                      />
                    ) : metadata.type === 'select' || field === '核查人' ? (
                      <Select
                        allowClear
                        showSearch={!mobile}
                        className="w-full"
                        size="large"
                        value={formValues[field] || undefined}
                        options={options}
                        onChange={value => {
                          setFormValues(current => ({ ...current, [field]: value || '' }))
                          if (field === data.workflow.result_field && value !== '待登记') {
                            setRegistrationPropertyId(undefined)
                            setRegistrationPropertyVersion(undefined)
                          }
                        }}
                      />
                    ) : (
                      <Input.TextArea
                        autoSize={{ minRows: field === '现住址' ? 2 : 3, maxRows: 7 }}
                        placeholder={field === '入住方式' ? '自购、房东出租、中介出租等' : undefined}
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
        open={manualConfirmOpen}
        title="人工确认登记"
        confirmLoading={manualConfirming}
        okText="确认并提交"
        cancelText="取消"
        onCancel={() => { if (!manualConfirming) setManualConfirmOpen(false) }}
        onOk={() => void confirmManualRegistration()}
      >
        <div className="grid gap-4">
          <Alert
            type="warning"
            showIcon
            message="请确认你已核对居住证有效登记"
            description="人工确认只允许用于地址不一致或同地址多套房屋的复核场景；系统仍会记录原因并等待腾讯写回结果。"
          />
          <label className="grid gap-1.5">
            <span className="text-sm font-medium text-[var(--app-text)]">确认原因</span>
            <Select
              value={manualConfirmReason}
              options={[
                { value: 'address_mismatch', label: '地址与房屋档案不一致，但已人工核对' },
                { value: 'address_ambiguous', label: '同地址存在多套房屋，已人工确认唯一房屋' },
              ]}
              onChange={value => setManualConfirmReason(value)}
            />
          </label>
          <label className="grid gap-1.5">
            <span className="text-sm font-medium text-[var(--app-text)]">复核备注（可选）</span>
            <Input.TextArea
              maxLength={500}
              showCount
              autoSize={{ minRows: 3, maxRows: 6 }}
              value={manualConfirmNote}
              onChange={event => setManualConfirmNote(event.target.value)}
              placeholder="填写必要的复核说明，不要粘贴身份证号、手机号或居住证返回原文"
            />
          </label>
        </div>
      </Modal>

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
                : '正在读取全民防任务和登记所需资料'}
            description={qmfPrepareResult
              ? (qmfPrepareResult.platform_task.result === '离开不返吴'
                ? '请逐项核对任务、社区和去往地。确认执行后会反馈全民防，提交后不能自动撤销。'
                : '请逐项核对人员、任务、操作人和照片。确认执行后会写入全民防，提交后不能自动撤销。')
              : '登记前核对不会执行写入；如需读取照片，照片只存在于本次认证响应和浏览器内存。'}
          />
          {qmfPreviewLoading && <Skeleton active paragraph={{ rows: 8 }} />}
          {qmfPreviewError && <Alert type="error" showIcon message={qmfPreviewError} />}

          {qmfPrepareResult && (() => {
            const preview = qmfPrepareResult
            return (
              <>
                {preview.person && preview.photo && (
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
                )}

                {preview.destination && (
                  <section className="qmf-preview-section">
                    <Descriptions
                      title="离开不返吴反馈信息"
                      size="small"
                      column={mobile ? 1 : 2}
                      items={[
                        { key: 'resolved-community', label: '平台正式社区', children: preview.destination.community || '未匹配' },
                        { key: 'qmf-community-code', label: '全民防社区代码', children: preview.destination.community_code || '未填写' },
                        { key: 'destination-code', label: '去往行政区划', children: preview.destination.area_code || '未识别' },
                        { key: 'destination-address', label: '去往地址详址', children: preview.destination.area_name || '未识别', span: mobile ? 1 : 2 },
                      ]}
                    />
                  </section>
                )}

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
                      ['community_code_valid', '全民防社区代码有效'],
                      ['destination_valid', '去往地信息有效'],
                    ].filter(([key]) => Object.prototype.hasOwnProperty.call(preview.checks, key)).map(([key, label]) => (
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
                <h3>执行状态</h3>
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
                <Alert type="error" showIcon message="外部结果无法确认，本条已冻结" description="请先到全民防人工核对；系统不会自动重试，也不能从头重放。" />
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
