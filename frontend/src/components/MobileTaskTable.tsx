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
import { useResponsiveLayout } from '../hooks/useResponsiveLayout'
import QmfFeedbackStatus from './QmfFeedbackStatus'
import { getResponsiveColumns, type ResponsiveColumns } from './responsiveTable'

const STATE_LABELS = {
  unchecked: { text: '未核查', color: 'red' },
  checked: { text: '待补结果', color: 'orange' },
  completed: { text: '已完成', color: 'green' },
} as const

interface MobileTaskTableProps {
  rows: MobileTaskItem[]
  loading: boolean
  analysisMode?: boolean
  selectionMode: boolean
  selectedRowKeys: Key[]
  canSelect: (task: MobileTaskItem) => boolean
  onSelect: (task: MobileTaskItem, selected: boolean) => void
  onOpen: (task: MobileTaskItem) => void
  onCopy: (value: string, label: '身份证号' | '手机号') => void
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
  loading,
  analysisMode = false,
  selectionMode,
  selectedRowKeys,
  canSelect,
  onSelect,
  onOpen,
  onCopy,
  onSaved,
}: MobileTaskTableProps) {
  const tableRef = useRef<HTMLDivElement>(null)
  const responsiveLayout = useResponsiveLayout(tableRef)
  const [editorItems, setEditorItems] = useState<Record<string, MobileTaskInlineEditorItem>>({})
  const [editorValues, setEditorValues] = useState<Record<string, Record<string, string>>>({})
  const [loadingEditorKeys, setLoadingEditorKeys] = useState<Set<string>>(new Set())
  const [savingRowKey, setSavingRowKey] = useState('')
  const editorItemsRef = useRef<Record<string, MobileTaskInlineEditorItem>>({})
  const loadingEditorKeysRef = useRef<Set<string>>(new Set())
  const editorElementsRef = useRef<Map<string, HTMLElement>>(new Map())
  const editorObserverRef = useRef<IntersectionObserver | null>(null)
  const pendingEditorKeysRef = useRef<Set<string>>(new Set())
  const editorFlushTimerRef = useRef<number | null>(null)
  const taskByKey = useMemo(
    () => new Map(rows.map(task => [task.task_key, task])),
    [rows],
  )
  const parserTypesKey = useMemo(
    () => [...new Set(rows.map(task => task.parser_type))].sort().join('|'),
    [rows],
  )
  const editorContext = `${analysisMode ? 'analysis' : 'tasks'}:${parserTypesKey}`
  const editorContextRef = useRef(editorContext)

  const requestEditors = useCallback(async (taskKeys: string[], force = false) => {
    const keys = [...new Set(taskKeys)].filter(taskKey => (
      taskKey
      && !loadingEditorKeysRef.current.has(taskKey)
      && (force || !editorItemsRef.current[taskKey])
    ))
    if (!keys.length) return
    const requestContext = editorContext
    keys.forEach(taskKey => loadingEditorKeysRef.current.add(taskKey))
    setLoadingEditorKeys(new Set(loadingEditorKeysRef.current))
    try {
      const grouped = keys.reduce<Record<string, Array<{ taskKey: string; rowKey: string }>>>((result, taskKey) => {
        const task = taskByKey.get(taskKey)
        if (task) (result[task.parser_type] ||= []).push({ taskKey, rowKey: task.row_key })
        return result
      }, {})
      const batches = Object.entries(grouped)
      const results = await Promise.all(
        batches.map(([parserType, parserTasks]) => (
          getMobileTaskInlineEditors(parserType, parserTasks.map(task => task.rowKey), analysisMode)
        )),
      )
      if (requestContext !== editorContextRef.current) return
      const values: Record<string, Record<string, string>> = {}
      const items: Record<string, MobileTaskInlineEditorItem> = {}
      batches.forEach(([, parserTasks], batchIndex) => {
        parserTasks.forEach(task => {
          const item = results[batchIndex].items[task.rowKey]
          if (item) items[task.taskKey] = item
        })
      })
      Object.entries(items).forEach(([taskKey, item]) => {
        const source = item.detail?.sources[0]
        if (source) values[taskKey] = { ...source.values }
      })
      setEditorItems(current => {
        const next = { ...current, ...items }
        editorItemsRef.current = next
        return next
      })
      setEditorValues(current => ({ ...current, ...values }))
    } catch (reason: any) {
      if (requestContext === editorContextRef.current) {
        message.error({
          key: 'mobile-task-inline-editor-load',
          content: errorMessage(reason, '当前可见任务的可编辑信息读取失败'),
        })
      }
    } finally {
      if (requestContext === editorContextRef.current) {
        keys.forEach(taskKey => loadingEditorKeysRef.current.delete(taskKey))
        setLoadingEditorKeys(new Set(loadingEditorKeysRef.current))
      }
    }
  }, [analysisMode, editorContext, taskByKey])

  const queueEditorLoad = useCallback((taskKey: string) => {
    if (
      !taskKey
      || editorItemsRef.current[taskKey]
      || loadingEditorKeysRef.current.has(taskKey)
      || pendingEditorKeysRef.current.has(taskKey)
    ) return
    pendingEditorKeysRef.current.add(taskKey)
    if (editorFlushTimerRef.current !== null) return
    editorFlushTimerRef.current = window.setTimeout(() => {
      editorFlushTimerRef.current = null
      const keys = [...pendingEditorKeysRef.current]
      pendingEditorKeysRef.current.clear()
      void requestEditors(keys)
    }, 60)
  }, [requestEditors])

  const setEditorElement = useCallback((taskKey: string, element: HTMLElement | null) => {
    const previous = editorElementsRef.current.get(taskKey)
    if (previous && previous !== element) editorObserverRef.current?.unobserve(previous)
    if (!element) {
      editorElementsRef.current.delete(taskKey)
      return
    }
    element.dataset.mobileTaskEditorRowKey = taskKey
    editorElementsRef.current.set(taskKey, element)
    editorObserverRef.current?.observe(element)
  }, [])

  useEffect(() => {
    editorContextRef.current = editorContext
    editorItemsRef.current = {}
    loadingEditorKeysRef.current.clear()
    pendingEditorKeysRef.current.clear()
    if (editorFlushTimerRef.current !== null) {
      window.clearTimeout(editorFlushTimerRef.current)
      editorFlushTimerRef.current = null
    }
    setEditorItems({})
    setEditorValues({})
    setLoadingEditorKeys(new Set())
    return () => {
      pendingEditorKeysRef.current.clear()
      if (editorFlushTimerRef.current !== null) {
        window.clearTimeout(editorFlushTimerRef.current)
        editorFlushTimerRef.current = null
      }
    }
  }, [editorContext])

  useEffect(() => {
    if (!parserTypesKey) return undefined
    if (typeof IntersectionObserver === 'undefined') {
      rows.slice(0, 20).forEach(task => queueEditorLoad(task.task_key))
      return undefined
    }
    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return
        const rowKey = (entry.target as HTMLElement).dataset.mobileTaskEditorRowKey
        if (rowKey) queueEditorLoad(rowKey)
      })
    }, { rootMargin: '600px 0px' })
    editorObserverRef.current = observer
    editorElementsRef.current.forEach(element => observer.observe(element))
    return () => {
      observer.disconnect()
      if (editorObserverRef.current === observer) editorObserverRef.current = null
    }
  }, [parserTypesKey, queueEditorLoad])

  const saveEditor = async (
    task: MobileTaskItem,
    item: MobileTaskInlineEditorItem,
    changes: Record<string, string>,
  ) => {
    const detail = item.detail
    const source = detail?.sources[0]
    if (!source || !Object.keys(changes).length || !detail?.writeback_enabled) return
    setSavingRowKey(task.task_key)
    try {
      const updater = analysisMode ? updateMobileTaskAnalysis : updateMobileTask
      const result = await updater(task.parser_type, source.id, {
        changes,
        base_values: Object.fromEntries(
          Object.keys(changes).map(field => [field, source.values[field] || '']),
        ),
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
        [task.task_key]: {
          ...item,
          detail: detail ? {
            ...detail,
            sources: [{ ...source, values: savedValues, revision: result.revision }],
          } : detail,
        },
      }))
      setEditorValues(current => ({ ...current, [task.task_key]: savedValues }))
      message.success(result.message)
      await onSaved()
    } catch (reason: any) {
      message.error(errorMessage(reason, '保存失败，请稍后重试'))
      if ([409, 502, 503].includes(Number(reason?.response?.status))) {
        await requestEditors([task.task_key], true)
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
    const item = editorItems[task.task_key]
    const detail = item?.detail
    const source = detail?.sources[0]
    const values = editorValues[task.task_key] || source?.values || {}
    const fields = detail && source
      ? mobileTaskEditorFields(detail, source.editable_fields, values, source.values)
      : []
    const changes = source ? buildMobileTaskChanges(source.values, values, fields) : {}
    const dirtyCount = Object.keys(changes).length
    const editorLoading = loadingEditorKeys.has(task.task_key)

    if (!item) {
      return (
        <div
          ref={element => setEditorElement(task.task_key, element)}
          className={`mobile-task-table-inline-editor ${toneClass} mobile-task-table-inline-editor--loading`}
          onClick={event => event.stopPropagation()}
        >
          <div className="mobile-task-table-inline-status">
            {editorLoading ? '正在准备本行填写项…' : '滚动到本行时自动读取可编辑信息'}
          </div>
          {!editorLoading && (
            <Button size="small" onClick={() => void requestEditors([task.task_key], true)}>读取本行</Button>
          )}
        </div>
      )
    }

    if (!item?.available || !detail || !source) {
      return (
        <div
          ref={element => setEditorElement(task.task_key, element)}
          className={`mobile-task-table-inline-editor ${toneClass} mobile-task-table-inline-editor--readonly`}
        >
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
        ref={element => setEditorElement(task.task_key, element)}
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
                  className={`mobile-task-table-inline-field${/地址|备注|研判/.test(field) ? ' mobile-task-table-inline-field--wide' : ''}`}
                >
                  <span>{field === '核查人' ? '任务分配' : field}</span>
                  {metadata.type === 'select' || field === '核查人' ? (
                    <Select
                      allowClear
                      showSearch
                      size="small"
                      placeholder="请选择"
                      disabled={selectionMode || savingRowKey === task.task_key}
                      value={values[field] || undefined}
                      options={options}
                      onChange={value => setEditorValues(current => ({
                        ...current,
                        [task.task_key]: { ...values, [field]: value || '' },
                      }))}
                      onBlur={() => void saveField(task, item, field, values[field] || '')}
                    />
                  ) : (
                    <Input.TextArea
                      size="small"
                      placeholder={field === '入住方式' ? '自购、房东出租、中介出租等' : '请输入'}
                      disabled={selectionMode || savingRowKey === task.task_key}
                      autoSize={{ minRows: 1, maxRows: 3 }}
                      value={values[field] || ''}
                      onChange={event => setEditorValues(current => ({
                        ...current,
                        [task.task_key]: { ...values, [field]: event.target.value },
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
          </div>
        )}
      </div>
    )
  }

  const columns: ResponsiveColumns<MobileTaskItem> = [
    {
      title: '截止日期',
      key: 'deadline',
      fixed: 'left',
      width: 100,
      responsivePriority: 'always',
      render: (_, task) => formatMobileTaskDeadline(task.summary.deadline) || '-',
    },
    {
      title: '社区',
      dataIndex: 'community',
      width: 105,
      responsivePriority: 'always',
      ellipsis: true,
      render: value => value || <span className="text-[var(--app-text-muted)]">未识别社区</span>,
    },
    {
      title: '核查人',
      dataIndex: 'inspector',
      width: 105,
      responsivePriority: 'always',
      ellipsis: true,
      render: value => value || <span className="text-[var(--app-text-muted)]">待分配</span>,
    },
    ...(rows.some(task => task.parser_type === '全链条') ? [{
      title: '来源',
      key: 'source',
      width: 130,
      responsivePriority: 'standard' as const,
      render: (_: unknown, task: MobileTaskItem) => {
        const sources = mobileTaskSourceTags(task.summary.source)
        return sources.length
          ? (
              <Tooltip title={sources.join('、')}>
                <div className="mobile-task-source-cloud mobile-task-source-cloud--table">
                  <div>
                    {sources.map(tag => (
                      <Tag key={`${task.task_key}-${tag}`} className="mobile-task-source-cloud__tag">{tag}</Tag>
                    ))}
                  </div>
                </div>
              </Tooltip>
            )
          : <span className="text-[var(--app-text-muted)]">未填写</span>
      },
    }] : []),
    {
      title: '姓名',
      key: 'name',
      width: 110,
      responsivePriority: 'always',
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
      responsivePriority: 'wide',
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
      responsivePriority: 'standard',
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
      responsivePriority: 'always',
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
      responsivePriority: 'wide',
      ellipsis: true,
      render: value => value || <span className="text-[var(--app-text-muted)]">未填写</span>,
    },
    {
      title: '状态',
      key: 'state',
      width: 190,
      responsivePriority: 'always',
      render: (_, task) => {
        const state = STATE_LABELS[task.state]
        return (
          <div className="flex flex-wrap gap-1">
            <Tag color={state.color}>{state.text}</Tag>
            {task.needs_review && <Tag color="warning" icon={<ExclamationCircleOutlined />}>需复核</Tag>}
            {task.review_stage === 'analyzed' && <Tag color="purple">已研判</Tag>}
            {(task.conflict || task.source_count > 1) && <Tag color="red">来源异常</Tag>}
            {task.sync_state === 'conflict' && <Tag color="red">同步冲突</Tag>}
            {task.sync_state === 'retry' && <Tag color="orange">同步重试</Tag>}
            {task.sync_state === 'pending' && <Tag color="blue">待同步</Tag>}
            {task.watch_marks?.map(mark => (
              <Tag key={`${task.task_key}-${mark.category_id}`} color={mark.color}>{mark.name}</Tag>
            ))}
            {task.qmf_status && <QmfFeedbackStatus status={task.qmf_status} compact />}
          </div>
        )
      },
    },
  ]

  const visibleColumns = useMemo(
    () => getResponsiveColumns(columns, responsiveLayout.mode) as TableColumnsType<MobileTaskItem>,
    [columns, responsiveLayout.mode],
  )
  const tableScrollWidth = responsiveLayout.isCompact
    ? 860
    : responsiveLayout.isStandard
      ? 1140
      : 1440

  return (
    <div ref={tableRef} className="app-card mobile-task-table overflow-hidden">
      <Table<MobileTaskItem>
        rowKey="task_key"
        size="middle"
        loading={loading}
        dataSource={rows}
        columns={visibleColumns}
        tableLayout="fixed"
        scroll={{ x: tableScrollWidth }}
        rowSelection={selectionMode ? {
          selectedRowKeys,
          hideSelectAll: true,
          columnWidth: 48,
          getCheckboxProps: task => ({ disabled: !canSelect(task) }),
          onSelect,
        } : undefined}
        expandable={{
          expandedRowKeys: rows.map(task => task.task_key),
          showExpandColumn: false,
          expandedRowRender: renderExpandedRow,
        }}
        pagination={false}
        onRow={task => ({
          'data-mobile-task-row-key': task.task_key,
          className: [
            'mobile-task-table-primary-row',
            `mobile-task-table-primary-row--tone-${mobileTaskSurfaceTone(task)}`,
            selectionMode && selectedRowKeys.includes(task.task_key) ? 'mobile-task-table-row-selected' : '',
          ].filter(Boolean).join(' '),
          onDoubleClick: () => onOpen(task),
        })}
      />
    </div>
  )
}
