import {
  CopyOutlined,
  EditOutlined,
  EyeOutlined,
  ExclamationCircleOutlined,
  SaveOutlined,
} from '@ant-design/icons'
import { Button, Input, Select, Table, Tag, Tooltip, message, type TableColumnsType } from 'antd'
import { useMemo, useRef, useState, type Key } from 'react'
import {
  getMobileTaskAnalysisDetail,
  getMobileTaskDetail,
  updateMobileTask,
  updateMobileTaskAnalysis,
  type MobileTaskDetailData,
  type MobileTaskItem,
} from '../api/client'
import {
  buildMobileTaskChanges,
  formatMobileTaskDeadline,
  mobileTaskEditorFields,
  mobileTaskPhoneOptions,
  mobileTaskSourceTags,
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

function ReadonlyField({
  label,
  value,
  onEdit,
}: {
  label: string
  value: string
  onEdit?: () => void
}) {
  const content = (
    <>
      <span>{label}</span>
      <strong title={value || '未填写'}>{value || '未填写'}</strong>
    </>
  )
  return onEdit ? (
    <button
      type="button"
      className="mobile-task-table-edit-field mobile-task-table-edit-field--clickable"
      onClick={event => {
        event.stopPropagation()
        onEdit()
      }}
    >
      {content}
    </button>
  ) : <div className="mobile-task-table-edit-field">{content}</div>
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
  const [editingRowKey, setEditingRowKey] = useState('')
  const [editorLoading, setEditorLoading] = useState(false)
  const [editorSaving, setEditorSaving] = useState(false)
  const [editorData, setEditorData] = useState<MobileTaskDetailData | null>(null)
  const [editorValues, setEditorValues] = useState<Record<string, string>>({})
  const editorRequestId = useRef(0)
  const editorSource = editorData?.sources[0] || null
  const editorFields = useMemo(() => (
    editorData && editorSource
      ? mobileTaskEditorFields(
          editorData,
          editorSource.editable_fields,
          editorValues,
          editorSource.values,
        )
      : []
  ), [editorData, editorSource, editorValues])
  const editorChanges = useMemo(() => (
    editorSource
      ? buildMobileTaskChanges(editorSource.values, editorValues, editorFields)
      : {}
  ), [editorFields, editorSource, editorValues])
  const editorDirty = Object.keys(editorChanges).length > 0

  const closeEditor = () => {
    editorRequestId.current += 1
    setEditingRowKey('')
    setEditorLoading(false)
    setEditorSaving(false)
    setEditorData(null)
    setEditorValues({})
  }

  const beginEdit = async (task: MobileTaskItem) => {
    if (selectionMode || editingRowKey === task.row_key) return
    if (editingRowKey && editorDirty && !window.confirm('当前行有未保存内容，确定放弃并编辑另一条任务吗？')) return
    if (task.conflict || task.source_count > 1) {
      message.info('该任务有多个来源，请进入详情选择需要修改的腾讯行')
      onOpen(task)
      return
    }

    const requestId = editorRequestId.current + 1
    editorRequestId.current = requestId
    setEditingRowKey(task.row_key)
    setEditorData(null)
    setEditorValues({})
    setEditorLoading(true)
    try {
      const detail = analysisMode
        ? await getMobileTaskAnalysisDetail(task.parser_type, task.row_key)
        : await getMobileTaskDetail(task.parser_type, task.row_key)
      if (requestId !== editorRequestId.current) return
      if (detail.sources.length !== 1) {
        closeEditor()
        message.info('该任务需要在详情页选择来源后再修改')
        onOpen(task)
        return
      }
      setEditorData(detail)
      setEditorValues({ ...detail.sources[0].values })
    } catch (reason: any) {
      if (requestId !== editorRequestId.current) return
      closeEditor()
      message.error(errorMessage(reason, '任务编辑信息读取失败'))
    } finally {
      if (requestId === editorRequestId.current) setEditorLoading(false)
    }
  }

  const saveEditor = async (task: MobileTaskItem) => {
    if (!editorSource || !editorDirty || !editorData?.writeback_enabled) return
    setEditorSaving(true)
    try {
      const updater = analysisMode ? updateMobileTaskAnalysis : updateMobileTask
      await updater(task.parser_type, editorSource.id, {
        changes: editorChanges,
        expected_revision: editorSource.revision,
      })
      message.success('任务已保存并写回腾讯表格')
      closeEditor()
      await onSaved()
    } catch (reason: any) {
      message.error(errorMessage(reason, '保存失败，请稍后重试'))
    } finally {
      setEditorSaving(false)
    }
  }

  const renderExpandedRow = (task: MobileTaskItem) => {
    const editing = editingRowKey === task.row_key
    if (!editing) {
      const edit = selectionMode ? undefined : () => void beginEdit(task)
      return (
        <div
          className="mobile-task-table-edit-grid"
          onDoubleClick={() => onOpen(task)}
        >
          <ReadonlyField label="现住址" value={task.summary.current_address} onEdit={edit} />
          <ReadonlyField label="核查结果" value={task.summary.result} onEdit={edit} />
          <ReadonlyField label="研判" value={task.summary.analysis} onEdit={edit} />
          <ReadonlyField label="二次反馈" value={task.summary.secondary_feedback} onEdit={edit} />
          <ReadonlyField label="调取照片" value={task.photo_fetched ? '已调照片' : '未调照片'} />
          <div className="mobile-task-table-edit-action">
            <Button
              size="small"
              type="primary"
              ghost
              icon={<EditOutlined />}
              disabled={selectionMode}
              onClick={event => {
                event.stopPropagation()
                void beginEdit(task)
              }}
            >编辑本行</Button>
          </div>
        </div>
      )
    }

    return (
      <div
        className="mobile-task-table-inline-editor"
        onClick={event => event.stopPropagation()}
        onDoubleClick={event => event.stopPropagation()}
      >
        {editorLoading ? (
          <div className="mobile-task-table-inline-status">正在读取可编辑字段和下拉选项…</div>
        ) : editorFields.length === 0 ? (
          <div className="mobile-task-table-inline-status">
            {editorData?.writeback_enabled ? '当前任务没有可编辑字段' : '在线回写已暂停，当前任务只能查看'}
          </div>
        ) : (
          <div className="mobile-task-table-inline-fields">
            {editorFields.map(field => {
              const metadata = editorSource?.cell_meta[field] || { type: 'text' }
              const options = metadata.options?.map(option => ({
                value: String(option.text),
                label: String(option.text),
              })) || []
              return (
                <label key={field} className="mobile-task-table-inline-field">
                  <span>{field === '核查人' ? '任务分配' : field}</span>
                  {metadata.type === 'select' || field === '核查人' ? (
                    <Select
                      allowClear
                      showSearch
                      size="small"
                      value={editorValues[field] || undefined}
                      options={options}
                      onChange={value => setEditorValues(current => ({ ...current, [field]: value || '' }))}
                    />
                  ) : (
                    <Input.TextArea
                      size="small"
                      autoSize={{ minRows: 1, maxRows: 3 }}
                      value={editorValues[field] || ''}
                      onChange={event => setEditorValues(current => ({ ...current, [field]: event.target.value }))}
                    />
                  )}
                </label>
              )
            })}
          </div>
        )}
        <div className="mobile-task-table-inline-actions">
          <Button size="small" disabled={editorSaving} onClick={closeEditor}>取消</Button>
          <Button
            size="small"
            type="primary"
            icon={<SaveOutlined />}
            loading={editorSaving}
            disabled={!editorDirty || !editorData?.writeback_enabled}
            onClick={() => void saveEditor(task)}
          >{editorDirty ? `保存 ${Object.keys(editorChanges).length} 项` : '没有修改'}</Button>
        </div>
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
          ? <Tooltip title={sources.join('、')}><span className="block truncate">{sources.join('、')}</span></Tooltip>
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
        const phone = phones[0] || task.summary.phone
        if (!phone) return <span className="text-[var(--app-text-muted)]">未填写</span>
        return (
          <Button
            type="link"
            size="small"
            className="h-auto p-0"
            icon={<CopyOutlined />}
            onClick={() => onCopy(phone, '手机号')}
          >
            {phone}{phones.length > 1 ? ` +${phones.length - 1}` : ''}
          </Button>
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
    {
      title: '操作',
      key: 'actions',
      fixed: 'right',
      width: 64,
      align: 'center',
      render: (_, task) => (
        <Tooltip title="查看任务">
          <Button
            type="text"
            icon={<EyeOutlined />}
            aria-label="查看任务"
            onClick={() => onOpen(task)}
          />
        </Tooltip>
      ),
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
        scroll={{ x: 1399 }}
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
            closeEditor()
            onPageChange(nextPage)
          },
        }}
        onRow={task => ({
          className: [
            'mobile-task-table-primary-row',
            selectionMode && selectedRowKeys.includes(task.row_key) ? 'mobile-task-table-row-selected' : '',
          ].filter(Boolean).join(' '),
          onDoubleClick: () => onOpen(task),
        })}
      />
    </div>
  )
}
