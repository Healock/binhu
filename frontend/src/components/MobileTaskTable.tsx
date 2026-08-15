import {
  CopyOutlined,
  ExclamationCircleOutlined,
} from '@ant-design/icons'
import { Button, Input, Select, Table, Tag, Tooltip, message, type TableColumnsType } from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState, type Key } from 'react'
import {
  getMobileTaskInlineEditors,
  updateMobileTask,
  updateMobileTaskAnalysis,
  type MobileTaskInlineEditorItem,
  type MobileTaskItem,
} from '../api/client'
import {
  buildMobileTaskChanges,
  formatMobileTaskDeadline,
  mergeMobileTaskSaveValues,
  mobileTaskEditorFields,
  mobileTaskPhoneOptions,
  mobileTaskSourceTags,
  mobileTaskSurfaceTone,
} from '../utils/mobileTasks'

const STATE_LABELS = {
  unchecked: { text: '未核查', color: 'red' },
  checked: { text: '待补结果', color: 'orange' },
  completed: { text: '已完成', color: 'green' },
} as const

interface MobileTaskTableProps {
  rows: MobileTaskItem[]
  total: number
  page: number
  loading: boolean
  analysisMode?: boolean
  selectionMode: boolean
  selectedRowKeys: Key[]
  canSelect: (task: MobileTaskItem) => boolean
  onSelect: (task: MobileTaskItem, selected: boolean) => void
  onOpen: (task: MobileTaskItem) => void
  onCopy: (value: string, label: '身份证号' | '手机号') => void
  onPageChange: (page: number) => void
  onSaved: () => Promise<void> | void
}

function errorMessage(reason: any, fallback: string) {
  const detail = reason?.response?.data?.detail
  return typeof detail === 'object'
    ? detail?.message || fallback
    : detail || reason?.message || fallback
}

export default function MobileTaskTable({
  rows,
  total,
  page,
  loading,
  analysisMode = false,
  selectionMode,
  selectedRowKeys,
  canSelect,
  onSelect,
  onOpen,
  onCopy,
  onPageChange,
  onSaved,
}: MobileTaskTableProps) {
  const [editorItems, setEditorItems] = useState<Record<string, MobileTaskInlineEditorItem>>({})
  const [editorValues, setEditorValues] = useState<Record<string, Record<string, string>>>({})
  const [editorsLoading, setEditorsLoading] = useState(false)
  const [savingRowKey, setSavingRowKey] = useState('')
  const editorRequestId = useRef(0)
  const rowSignature = useMemo(
    () => rows.map(task => `${task.parser_type}:${task.row_key}`).join('|'),
    [rows],
  )

  const loadEditors = useCallback(async () => {
    const requestId = editorRequestId.current + 1
    editorRequestId.current = requestId
    if (!rows.length) {
      setEditorItems({})
      setEditorValues({})
      setEditorsLoading(false)
      return
    }
    setEditorsLoading(true)
    setEditorItems({})
    setEditorValues({})
    try {
      const result = await getMobileTaskInlineEditors(
        rows[0].parser_type,
        rows.map(task => task.row_key),
        analysisMode,
      )
      if (requestId !== editorRequestId.current) return
      const values: Record<string, Record<string, string>> = {}
      Object.entries(result.items).forEach(([rowKey, item]) => {
        const source = item.detail?.sources[0]
        if (source) values[rowKey] = { ...source.values }
      })
      setEditorItems(result.items)
      setEditorValues(values)
    } catch (reason: any) {
      if (requestId === editorRequestId.current) {
        message.error(errorMessage(reason, '当前页可编辑信息读取失败'))
      }
    } finally {
      if (requestId === editorRequestId.current) setEditorsLoading(false)
    }
  }, [analysisMode, rowSignature])

  useEffect(() => {
    void loadEditors()
    return () => {
      editorRequestId.current += 1
    }
  }, [loadEditors])

  const saveEditor = async (
    task: MobileTaskItem,
    item: MobileTaskInlineEditorItem,
    changes: Record<string, string>,
  ) => {
    const detail = item.detail
    const source = detail?.sources[0]
    if (!source || !Object.keys(changes).length || !detail?.writeback_enabled) return
    setSavingRowKey(task.row_key)
    try {
      const updater = analysisMode ? updateMobileTaskAnalysis : updateMobileTask
      const result = await updater(task.parser_type, source.id, {
        changes,
        expected_revision: source.revision,
      })
      const savedValues = mergeMobileTaskSaveValues(
        source.values,
        changes,
        result.values,
        source.cell_meta,
      )
      setEditorItems(current => ({
        ...current,
        [task.row_key]: {
          ...item,
          detail: detail ? {
            ...detail,
            sources: [{ ...source, values: savedValues, revision: result.revision }],
          } : detail,
        },
      }))
      setEditorValues(current => ({ ...current, [task.row_key]: savedValues }))
      message.success('已自动保存并写回腾讯表格')
      await onSaved()
    } catch (reason: any) {
      message.error(errorMessage(reason, '保存失败，请稍后重试'))
      if ([409, 502, 503].includes(Number(reason?.response?.status))) {
        await loadEditors()
      }
    } finally {
      setSavingRowKey('')
    }
  }

  const saveField = async (
    task: MobileTaskItem,
    item: MobileTaskInlineEditorItem,
    field: string,
    value: string,
  ) => {
    const source = item.detail?.sources[0]
    if (!source) return
    const changes = buildMobileTaskChanges(
      source.values,
      { ...source.values, [field]: value },
      [field],
    )
    if (Object.keys(changes).length) {
      await saveEditor(task, item, changes)
    }
  }

  const renderExpandedRow = (task: MobileTaskItem) => {
    const surfaceTone = mobileTaskSurfaceTone(task)
    const toneClass = `mobile-task-table-inline-editor--tone-${surfaceTone}`
    const item = editorItems[task.row_key]
    const detail = item?.detail
    const source = detail?.sources[0]
    const values = editorValues[task.row_key] || source?.values || {}
    const fields = detail && source
      ? mobileTaskEditorFields(detail, source.editable_fields, values, source.values)
      : []
    const changes = source ? buildMobileTaskChanges(source.values, values, fields) : {}
    const dirtyCount = Object.keys(changes).length

    if (editorsLoading && !item) {
      return (
        <div className={`mobile-task-table-inline-editor ${toneClass} mobile-task-table-inline-editor--loading`}>
          <div className="mobile-task-table-inline-status">正在准备本行填写项…</div>
        </div>
      )
    }

    if (!item?.available || !detail || !source) {
      return (
        <div className={`mobile-task-table-inline-editor ${toneClass} mobile-task-table-inline-editor--readonly`}>
          <div className="mobile-task-table-inline-fields">
            <div className="mobile-task-table-inline-readonly"><span>现住址</span><strong>{task.summary.current_address || '未填写'}</strong></div>
            <div className="mobile-task-table-inline-readonly"><span>核查结果</span><strong>{task.summary.result || '未填写'}</strong></div>
            <div className="mobile-task-table-inline-readonly">
              <span>研判</span>
              <strong>
                <Tooltip title={task.summary.analysis || '未填写'}>
                  <span className="block truncate">{task.summary.analysis || '未填写'}</span>
                </Tooltip>
              </strong>
            </div>
            <div className="mobile-task-table-inline-readonly"><span>二次反馈</span><strong>{task.summary.secondary_feedback || '未填写'}</strong></div>
            <div className="mobile-task-table-inline-readonly"><span>调取照片</span><strong>{task.photo_fetched ? '已调照片' : '未调照片'}</strong></div>
          </div>
          <div className="mobile-task-table-inline-actions">
            <Tooltip title={item?.reason || '当前任务只能在详情中处理'}>
              <Button size="small" onClick={() => onOpen(task)}>进入详情</Button>
            </Tooltip>
          </div>
        </div>
      )
    }

    return (
      <div
        className={`mobile-task-table-inline-editor ${toneClass}${dirtyCount ? ' mobile-task-table-inline-editor--dirty' : ''}`}
        onClick={event => event.stopPropagation()}
        onDoubleClick={event => event.stopPropagation()}
      >
        {fields.length === 0 ? (
          <div className="mobile-task-table-inline-status">
            {detail.writeback_enabled ? '当前任务没有可填写字段' : '在线回写已暂停，当前任务只能查看'}
          </div>
        ) : (
          <div className="mobile-task-table-inline-fields">
            {fields.map(field => {
              const metadata = source.cell_meta[field] || { type: 'text' }
              const options = metadata.options?.map(option => ({
                value: String(option.text),
                label: String(option.text),
              })) || []
              return (
                <label
                  key={field}
                  className={`mobile-task-table-inline-field${/地址|反馈|备注|研判/.test(field) ? ' mobile-task-table-inline-field--wide' : ''}`}
                >
                  <span>{field === '核查人' ? '任务分配' : field}</span>
                  {metadata.type === 'select' || field === '核查人' ? (
                    <Select
                      allowClear
                      showSearch
                      size="small"
                      placeholder="请选择"
                      disabled={selectionMode || savingRowKey === task.row_key}
                      value={values[field] || undefined}
                      options={options}
                      onChange={value => setEditorValues(current => ({
                        ...current,
                        [task.row_key]: { ...values, [field]: value || '' },
                      }))}
                      onBlur={() => void saveField(task, item, field, values[field] || '')}
                    />
                  ) : (
                    <Input.TextArea
                      size="small"
                      placeholder="请输入"
                      disabled={selectionMode || savingRowKey === task.row_key}
                      autoSize={{ minRows: 1, maxRows: 3 }}
                      value={values[field] || ''}
                      onChange={event => setEditorValues(current => ({
                        ...current,
                        [task.row_key]: { ...values, [field]: event.target.value },
                      }))}
                      onBlur={() => void saveField(task, item, field, values[field] || '')}
                    />
                  )}
                </label>
              )
            })}
            {!analysisMode && !fields.includes('研判') && task.summary.analysis && (
              <div className="mobile-task-table-inline-readonly">
                <span>研判</span>
                <strong>
                  <Tooltip title={task.summary.analysis}>
                    <span className="block truncate">{task.summary.analysis}</span>
                  </Tooltip>
                </strong>
              </div>
            )}
            <div className="mobile-task-table-inline-readonly mobile-task-table-inline-readonly--photo">
              <span>调取照片</span>
              <strong>{task.photo_fetched ? '已调照片' : '未调照片'}</strong>
            </div>
          </div>
        )}
      </div>
    )
  }

  const columns: TableColumnsType<MobileTaskItem> = [
    {
      title: '截止日期',
      key: 'deadline',
      fixed: 'left',
      width: 100,
      render: (_, task) => formatMobileTaskDeadline(task.summary.deadline) || '-',
    },
    {
      title: '社区',
      dataIndex: 'community',
      width: 105,
      ellipsis: true,
      render: value => value || <span className="text-[var(--app-text-muted)]">未识别社区</span>,
    },
    {
      title: '核查人',
      dataIndex: 'inspector',
      width: 105,
      ellipsis: true,
      render: value => value || <span className="text-[var(--app-text-muted)]">待分配</span>,
    },
    {
      title: '来源',
      key: 'source',
      width: 130,
      render: (_, task) => {
        const sources = mobileTaskSourceTags(task.summary.source)
        return sources.length
          ? (
              <Tooltip title={sources.join('、')}>
                <div className="mobile-task-source-cloud mobile-task-source-cloud--table">
                  <div>
                    {sources.map(tag => (
                      <Tag key={`${task.row_key}-${tag}`} className="mobile-task-source-cloud__tag">{tag}</Tag>
                    ))}
                  </div>
                </div>
              </Tooltip>
            )
          : <span className="text-[var(--app-text-muted)]">未填写</span>
      },
    },
    {
      title: '姓名',
      key: 'name',
      width: 110,
      render: (_, task) => (
        <button
          type="button"
          className="block max-w-full truncate text-left font-medium text-[var(--app-text-strong)] hover:text-[var(--app-primary)]"
          title={task.summary.title}
          onClick={() => onOpen(task)}
        >
          {task.summary.title || '未填写姓名'}
        </button>
      ),
    },
    {
      title: '身份证号码',
      key: 'identity_number',
      width: 190,
      render: (_, task) => task.summary.identity_number ? (
        <Button
          type="link"
          size="small"
          className="h-auto max-w-full p-0 text-xs"
          icon={<CopyOutlined />}
          onClick={() => onCopy(task.summary.identity_number, '身份证号')}
        >
          <span className="truncate">{task.summary.identity_number}</span>
        </Button>
      ) : <span className="text-[var(--app-text-muted)]">未填写</span>,
    },
    {
      title: '电话',
      key: 'phone',
      width: 150,
      render: (_, task) => {
        const phones = mobileTaskPhoneOptions(task.summary.phone)
        const visiblePhones = phones.slice(0, 3)
        if (!visiblePhones.length) return <span className="text-[var(--app-text-muted)]">未填写</span>
        return (
          <div className="flex flex-col items-start">
            {visiblePhones.map(phone => (
              <Button
                key={phone}
                type="link"
                size="small"
                className="h-auto max-w-full p-0"
                icon={<CopyOutlined />}
                onClick={() => onCopy(phone, '手机号')}
              >
                <span className="truncate">{phone}</span>
              </Button>
            ))}
            {phones.length > visiblePhones.length && (
              <span className="pl-5 text-xs text-[var(--app-text-secondary)]">
                +{phones.length - visiblePhones.length}
              </span>
            )}
          </div>
        )
      },
    },
    {
      title: '地址',
      key: 'address',
      width: 250,
      ellipsis: true,
      render: (_, task) => {
        const address = task.summary.original_address || '未填写'
        return <Tooltip title={address}><span>{address}</span></Tooltip>
      },
    },
    {
      title: '登记情况',
      dataIndex: ['summary', 'registration_status'],
      width: 110,
      ellipsis: true,
      render: value => value || <span className="text-[var(--app-text-muted)]">未填写</span>,
    },
    {
      title: '状态',
      key: 'state',
      width: 190,
      render: (_, task) => {
        const state = STATE_LABELS[task.state]
        return (
          <div className="flex flex-wrap gap-1">
            <Tag color={state.color}>{state.text}</Tag>
            {task.needs_review && <Tag color="warning" icon={<ExclamationCircleOutlined />}>需复核</Tag>}
            {task.review_stage === 'analyzed' && <Tag color="purple">已研判</Tag>}
            {task.photo_fetched && <Tag color="green">已调照片</Tag>}
            {(task.conflict || task.source_count > 1) && <Tag color="red">来源异常</Tag>}
            {task.pending_sync && <Tag color="blue">待同步</Tag>}
            {task.watch_marks?.map(mark => (
              <Tag key={`${task.row_key}-${mark.category_id}`} color={mark.color}>{mark.name}</Tag>
            ))}
          </div>
        )
      },
    },
  ]

  return (
    <div className="app-card mobile-task-table overflow-hidden">
      <Table<MobileTaskItem>
        rowKey="row_key"
        size="middle"
        loading={loading}
        dataSource={rows}
        columns={columns}
        tableLayout="fixed"
        scroll={{ x: 1440 }}
        rowSelection={selectionMode ? {
          selectedRowKeys,
          hideSelectAll: true,
          columnWidth: 48,
          getCheckboxProps: task => ({ disabled: !canSelect(task) }),
          onSelect,
        } : undefined}
        expandable={{
          expandedRowKeys: rows.map(task => task.row_key),
          showExpandColumn: false,
          expandedRowRender: renderExpandedRow,
        }}
        pagination={{
          current: page,
          pageSize: 50,
          total,
          showSizeChanger: false,
          showTotal: count => `共 ${count} 条`,
          onChange: nextPage => {
            onPageChange(nextPage)
          },
        }}
        onRow={task => ({
          className: [
            'mobile-task-table-primary-row',
            `mobile-task-table-primary-row--tone-${mobileTaskSurfaceTone(task)}`,
            selectionMode && selectedRowKeys.includes(task.row_key) ? 'mobile-task-table-row-selected' : '',
          ].filter(Boolean).join(' '),
          onDoubleClick: () => onOpen(task),
        })}
      />
    </div>
  )
}
