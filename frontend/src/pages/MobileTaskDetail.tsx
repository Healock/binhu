import {
  ArrowLeftOutlined,
  CameraOutlined,
  DownloadOutlined,
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
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  getMobileTaskDetail,
  getMobileTaskAnalysisDetail,
  updateMobileTask,
  updateMobileTaskAnalysis,
  workflowApi,
  type MobileTaskDetailData,
  type MobileTaskSource,
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

const STATE_LABELS = {
  unchecked: { text: '未核查', color: 'red' },
  checked: { text: '待补结果', color: 'orange' },
  completed: { text: '已完成', color: 'green' },
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
  const { recordActivity, user } = useAuth()
  const { parserType = '', rowKey = '' } = useParams()
  const [data, setData] = useState<MobileTaskDetailData | null>(null)
  const [selectedSourceId, setSelectedSourceId] = useState<number | null>(null)
  const [formValues, setFormValues] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [savedMessage, setSavedMessage] = useState('')
  const [photoRequestOpen, setPhotoRequestOpen] = useState(false)
  const [photoSubmitting, setPhotoSubmitting] = useState(false)

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
      setSavedMessage('已保存，滨湖平台数据已同步并写回腾讯表格')
    } catch (reason: any) {
      const status = reason?.response?.status
      setError(detailError(reason, '保存失败，请稍后重试'))
      if (status === 409 || status === 502) await load(selectedSource.id)
    } finally {
      setSaving(false)
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

  return (
    <div className="mobile-task-page mobile-task-detail-page">
      <div className="flex items-center justify-between gap-3">
        <Button type="text" className="min-h-11 px-1" icon={<ArrowLeftOutlined />} onClick={() => { if (confirmPendingNavigation()) navigate(-1) }}>返回</Button>
        <div className="flex items-center gap-2">
          <Tag color={state.color}>{state.text}</Tag>
          {data.task.pending_sync && <Tag color="blue">待同步</Tag>}
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
                  <span>腾讯第 {source.physical_row} 行</span>
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
            <span className="text-xs text-[var(--app-text-muted)]">腾讯第 {selectedSource.physical_row} 行</span>
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
                        showSearch
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
    </div>
  )
}
