import {
  ArrowLeftOutlined,
  CopyOutlined,
  PhoneOutlined,
  SaveOutlined,
} from '@ant-design/icons'
import {
  Alert,
  Button,
  Collapse,
  Descriptions,
  Empty,
  Input,
  Select,
  Skeleton,
  Tag,
  message,
} from 'antd'
import { useNavigate, useParams } from 'react-router-dom'
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  getMobileTaskDetail,
  updateMobileTask,
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
  mobileTaskEditorFields,
  mobileTaskPhoneValue,
  mobileTaskSourceDifferences,
  mobileTaskSourceNeedsReview,
  mobileTaskSourceState,
} from '../utils/mobileTasks'

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

export default function MobileTaskDetail() {
  const navigate = useNavigate()
  const { recordActivity } = useAuth()
  const { parserType = '', rowKey = '' } = useParams()
  const [data, setData] = useState<MobileTaskDetailData | null>(null)
  const [selectedSourceId, setSelectedSourceId] = useState<number | null>(null)
  const [formValues, setFormValues] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [savedMessage, setSavedMessage] = useState('')

  const selectedSource = useMemo(
    () => data?.sources.find(source => source.id === selectedSourceId) || null,
    [data, selectedSourceId],
  )
  const visibleEditorFields = useMemo(() => (
    data && selectedSource
      ? mobileTaskEditorFields(data, selectedSource.editable_fields, formValues)
      : []
  ), [data, formValues, selectedSource])
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
      const result = await getMobileTaskDetail(parserType, rowKey)
      setData(result)
      const source = result.sources.find(item => item.id === preferredSourceId) || result.sources[0]
      if (source) selectSource(source)
    } catch (reason: any) {
      setError(detailError(reason, '任务详情读取失败'))
    } finally {
      setLoading(false)
    }
  }, [parserType, rowKey, selectSource])

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
      const result = await updateMobileTask(parserType, selectedSource.id, {
        changes,
        expected_revision: selectedSource.revision,
      })
      setData(current => current ? {
        ...current,
        task: { ...current.task, pending_sync: true },
        sources: current.sources.map(source => source.id === selectedSource.id ? {
          ...source,
          values: result.values,
          revision: result.revision,
          state: mobileTaskSourceState(
            parserType,
            current.workflow.result_field,
            result.values,
          ),
          needs_review: mobileTaskSourceNeedsReview(
            current.workflow.result_field,
            current.workflow.secondary_fields,
            result.values,
          ),
        } : source),
      } : current)
      setFormValues({ ...result.values })
      setSavedMessage('已写回腾讯表格，汇总待同步')
    } catch (reason: any) {
      const status = reason?.response?.status
      setError(detailError(reason, '保存失败，请稍后重试'))
      if (status === 409 || status === 502) await load(selectedSource.id)
    } finally {
      setSaving(false)
    }
  }

  const copy = async (value: string, label: string) => {
    await navigator.clipboard.writeText(value)
    message.success(`${label}已复制`)
  }

  const dial = async (phone: string) => {
    await recordActivity().catch(() => {})
    window.location.href = `tel:${mobileTaskPhoneValue(phone)}`
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
  const address = selectedSource ? firstValue(selectedSource.values, data.workflow.address_fields) : data.task.summary.address

  return (
    <div className="mobile-task-page">
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

        {(phone || address) && (
          <div className="mt-4 grid grid-cols-2 gap-2">
            {phone && <Button className="min-h-11" type="primary" icon={<PhoneOutlined />} onClick={() => void dial(phone)}>拨打电话</Button>}
            {phone && <Button className="min-h-11" icon={<CopyOutlined />} onClick={() => void copy(phone, '电话')}>复制电话</Button>}
            {address && <Button className="col-span-2 min-h-11" icon={<CopyOutlined />} onClick={() => void copy(address, '地址')}>复制地址</Button>}
          </div>
        )}
      </section>

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
              <h2 className="font-semibold text-[var(--app-text-strong)]">核查处理</h2>
              <p className="mt-0.5 text-xs text-[var(--app-text-secondary)]">确认所有修改后统一保存</p>
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
                    <span className="mb-1.5 block text-sm font-medium text-[var(--app-text)]">{field}</span>
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
    </div>
  )
}
