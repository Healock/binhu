import {
  Alert,
  Button,
  DatePicker,
  Empty,
  Input,
  InputNumber,
  Modal,
  Segmented,
  Space,
  Table,
  Tag,
  Tooltip,
  message,
} from 'antd'
import type { TableColumnsType } from 'antd'
import {
  CloudDownloadOutlined,
  CopyOutlined,
  DeleteOutlined,
  FolderOpenOutlined,
  PlusOutlined,
  ReloadOutlined,
  SwapOutlined,
  UndoOutlined,
} from '@ant-design/icons'
import axios from 'axios'
import dayjs, { type Dayjs } from 'dayjs'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Navigate, useNavigate, useSearchParams } from 'react-router-dom'
import {
  createWorkLogDraft,
  exportWorkLog,
  getWorkLogDraft,
  getWorkLogMissing,
  getWorkLogSchema,
  refreshWorkLogDraft,
  saveWorkLogDraft,
  takeoverWorkLogDraft,
} from '../api/client'
import { PageHeader, Panel } from '../components/ui'
import { useAuth } from '../context/AuthContext'
import type {
  WorkLogBlock,
  WorkLogColumn,
  WorkLogDraft,
  WorkLogField,
  WorkLogSchema,
} from '../types'
import {
  deriveWorkLogValues,
  leafWorkLogColumns,
} from '../utils/workLog'

type Values = Record<string, unknown>
type Row = Record<string, unknown>
type SaveState = 'idle' | 'saving' | 'saved' | 'failed' | 'conflict'

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

function errorMessage(error: unknown, fallback: string) {
  if (axios.isAxiosError(error)) {
    return error.response?.data?.detail || fallback
  }
  return error instanceof Error ? error.message : fallback
}

function FieldInput({
  field,
  value,
  disabled,
  compact = false,
  onChange,
}: {
  field: WorkLogField
  value: unknown
  disabled: boolean
  compact?: boolean
  onChange: (value: unknown) => void
}) {
  if (field.type === 'textarea') {
    return (
      <Input.TextArea
        value={value == null ? '' : String(value)}
        disabled={disabled}
        autoSize={{ minRows: 3, maxRows: 12 }}
        onChange={event => onChange(event.target.value)}
      />
    )
  }
  if (field.type === 'text') {
    return (
      <Input
        value={value == null ? '' : String(value)}
        disabled={disabled}
        style={{ width: compact ? field.width || 120 : '100%' }}
        onChange={event => onChange(event.target.value)}
      />
    )
  }
  return (
    <InputNumber
      value={value == null || value === '' ? null : Number(value)}
      disabled={disabled}
      min={0}
      precision={field.precision ?? (field.type === 'number' ? 0 : 1)}
      suffix={field.type === 'percent' ? '%' : undefined}
      style={{ width: compact ? field.width || 88 : '100%' }}
      onChange={onChange}
    />
  )
}

function InlineField({
  field,
  value,
  disabled,
  overridden,
  onChange,
  onRestore,
}: {
  field: WorkLogField
  value: unknown
  disabled: boolean
  overridden: boolean
  onChange: (value: unknown) => void
  onRestore: () => void
}) {
  const automatic = field.source !== 'manual'
  return (
    <span className="mx-1 inline-flex items-center gap-0.5 align-middle">
      <Tooltip
        title={automatic
          ? overridden ? '已人工修改' : '由系统自动填写'
          : field.label}
      >
        <span className={automatic
          ? overridden
            ? 'rounded-md ring-1 ring-amber-300'
            : 'rounded-md ring-1 ring-blue-200'
          : ''}
        >
          <FieldInput
            field={field}
            value={value}
            disabled={disabled}
            compact
            onChange={onChange}
          />
        </span>
      </Tooltip>
      {automatic && overridden && !disabled && (
        <Tooltip title="恢复自动计算">
          <Button
            type="text"
            size="small"
            icon={<UndoOutlined />}
            aria-label={`恢复${field.label}的系统值`}
            onClick={onRestore}
          />
        </Tooltip>
      )}
    </span>
  )
}

function TableCell({
  definition,
  value,
  disabled,
  onChange,
}: {
  definition: WorkLogColumn
  value: unknown
  disabled: boolean
  onChange: (value: unknown) => void
}) {
  const type = definition.type || 'text'
  if (type === 'textarea') {
    return (
      <Input.TextArea
        value={value == null ? '' : String(value)}
        disabled={disabled}
        autoSize={{ minRows: 1, maxRows: 6 }}
        onChange={event => onChange(event.target.value)}
      />
    )
  }
  if (type === 'text') {
    return (
      <Input
        value={value == null ? '' : String(value)}
        disabled={disabled}
        onChange={event => onChange(event.target.value)}
      />
    )
  }
  return (
    <InputNumber
      value={value == null || value === '' ? null : Number(value)}
      disabled={disabled}
      min={0}
      precision={type === 'number' ? 0 : 1}
      suffix={type === 'percent' ? '%' : undefined}
      className="w-full"
      onChange={onChange}
    />
  )
}

function EditableWorkLogTable({
  field,
  help,
  value,
  disabled,
  overridden,
  onChange,
  onRestore,
}: {
  field: WorkLogField
  help?: string
  value: unknown
  disabled: boolean
  overridden: boolean
  onChange: (value: unknown) => void
  onRestore: () => void
}) {
  const rows = Array.isArray(value) ? value as Row[] : []
  const leaves = leafWorkLogColumns(field.columns || [])
  const detailRows = field.row_mode === 'detail'
  const setCell = (index: number, key: string, nextValue: unknown) => {
    onChange(rows.map((row, rowIndex) => (
      rowIndex === index ? { ...row, [key]: nextValue } : row
    )))
  }
  const buildColumns = (
    definitions: WorkLogColumn[],
  ): TableColumnsType<Row> => definitions.map(definition => {
    if (definition.children?.length) {
      return {
        title: definition.label,
        children: buildColumns(definition.children),
      }
    }
    const key = definition.key || ''
    return {
      title: definition.label,
      dataIndex: key,
      width: definition.width || 100,
      render: (_: unknown, __: Row, index: number) => (
        <TableCell
          definition={definition}
          value={rows[index]?.[key]}
          disabled={disabled}
          onChange={nextValue => setCell(index, key, nextValue)}
        />
      ),
    }
  })
  const columns = buildColumns(field.columns || [])
  if (detailRows) {
    columns.push({
      title: '操作',
      fixed: 'right',
      width: 84,
      render: (_: unknown, row: Row, index: number) => (
        <Space size={0}>
          <Tooltip title="复制本行">
            <Button
              type="text"
              icon={<CopyOutlined />}
              disabled={disabled}
              aria-label={`复制第 ${index + 1} 行`}
              onClick={() => {
                const next = [...rows]
                next.splice(index + 1, 0, { ...row })
                onChange(next)
              }}
            />
          </Tooltip>
          <Tooltip title="删除本行">
            <Button
              type="text"
              danger
              icon={<DeleteOutlined />}
              disabled={disabled}
              aria-label={`删除第 ${index + 1} 行`}
              onClick={() => onChange(
                rows.filter((_, rowIndex) => rowIndex !== index),
              )}
            />
          </Tooltip>
        </Space>
      ),
    })
  }

  return (
    <div className="my-4 rounded-xl border border-slate-200 bg-slate-50/40 p-3 dark:border-slate-700 dark:bg-slate-900/30">
      <div className="mb-2 flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="flex flex-wrap items-center gap-2 font-semibold">
            <span>{field.label}</span>
            {field.source !== 'manual' && (
              <Tag color={overridden ? 'orange' : 'blue'}>
                {overridden ? '人工修改' : '系统数据'}
              </Tag>
            )}
          </div>
          {help && <div className="mt-1 text-xs text-slate-500">{help}</div>}
        </div>
        {field.source !== 'manual' && overridden && !disabled && (
          <Button icon={<UndoOutlined />} onClick={onRestore}>
            恢复整张系统表
          </Button>
        )}
      </div>
      <Table<Row>
        rowKey={(_, index) => String(index)}
        size="small"
        bordered
        pagination={false}
        dataSource={rows}
        columns={columns}
        scroll={{ x: Math.max(
          760,
          leaves.reduce((total, item) => total + (item.width || 100), 0)
            + (detailRows ? 84 : 0),
        ) }}
        locale={{ emptyText: '暂无数据，可按需要添加' }}
      />
      {detailRows && !disabled && (
        <Button
          className="mt-3"
          icon={<PlusOutlined />}
          onClick={() => {
            const empty = Object.fromEntries(
              leaves.map(column => [column.key || '', '']),
            )
            onChange([...rows, empty])
          }}
        >
          添加一行
        </Button>
      )}
    </div>
  )
}

function WorkLogBlockView({
  block,
  values,
  disabled,
  overrides,
  onChange,
  onRestore,
}: {
  block: WorkLogBlock
  values: Values
  disabled: boolean
  overrides: Values
  onChange: (field: WorkLogField, value: unknown) => void
  onRestore: (fieldId: string) => void
}) {
  if (block.type === 'heading') {
    return block.level >= 3
      ? <h3 className="mb-2 mt-5 text-base font-semibold">{block.title}</h3>
      : <h2 className="mb-2 mt-4 text-lg font-semibold">{block.title}</h2>
  }
  if (block.type === 'sentence') {
    return (
      <div className="my-3 flex flex-wrap items-center gap-y-2 text-sm leading-9 text-slate-800 dark:text-slate-100 md:text-base">
        {block.segments.map((segment, index) => (
          typeof segment === 'string'
            ? <span key={`${index}-${segment}`}>{segment}</span>
            : (
                <InlineField
                  key={segment.id}
                  field={segment}
                  value={values[segment.id]}
                  disabled={disabled}
                  overridden={Object.prototype.hasOwnProperty.call(
                    overrides,
                    segment.id,
                  )}
                  onChange={value => onChange(segment, value)}
                  onRestore={() => onRestore(segment.id)}
                />
              )
        ))}
      </div>
    )
  }
  if (block.type === 'textarea') {
    const field = block.field
    return (
      <div className="my-4">
        <div className="mb-1.5 flex items-center gap-2 text-sm font-medium">
          <span>{field.label}</span>
          {field.required && <span className="text-red-500">*</span>}
        </div>
        <FieldInput
          field={field}
          value={values[field.id]}
          disabled={disabled}
          onChange={value => onChange(field, value)}
        />
      </div>
    )
  }
  return (
    <EditableWorkLogTable
      field={block.field}
      help={block.help}
      value={values[block.field.id]}
      disabled={disabled}
      overridden={Object.prototype.hasOwnProperty.call(
        overrides,
        block.field.id,
      )}
      onChange={value => onChange(block.field, value)}
      onRestore={() => onRestore(block.field.id)}
    />
  )
}

export default function WorkLog() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [schema, setSchema] = useState<WorkLogSchema | null>(null)
  const [reportType, setReportType] = useState('daily')
  const [businessDate, setBusinessDate] = useState<Dayjs>(() => {
    const queryDate = searchParams.get('date') || ''
    const parsed = dayjs(queryDate)
    return /^\d{4}-\d{2}-\d{2}$/.test(queryDate) && parsed.isValid()
      ? parsed
      : dayjs()
  })
  const [draft, setDraft] = useState<WorkLogDraft | null>(null)
  const [manualValues, setManualValues] = useState<Values>({})
  const [overrideValues, setOverrideValues] = useState<Values>({})
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const [dirty, setDirty] = useState(false)
  const [changeSequence, setChangeSequence] = useState(0)
  const [exporting, setExporting] = useState(false)
  const savingRef = useRef(false)
  const latestSequence = useRef(0)
  const manualRef = useRef<Values>({})
  const overrideRef = useRef<Values>({})
  const [modal, contextHolder] = Modal.useModal()

  useEffect(() => {
    getWorkLogSchema()
      .then(setSchema)
      .catch(error => message.error(errorMessage(error, '读取工作日志字段失败')))
  }, [])

  const applyDraft = useCallback((next: WorkLogDraft) => {
    setDraft(next)
    setManualValues(next.manual_values || {})
    setOverrideValues(next.override_values || {})
    manualRef.current = next.manual_values || {}
    overrideRef.current = next.override_values || {}
    setDirty(false)
    setSaveState('saved')
  }, [])

  const loadDraft = useCallback(async (selectedDate: string) => {
    setLoading(true)
    setDraft(null)
    setDirty(false)
    setSaveState('idle')
    try {
      applyDraft(await getWorkLogDraft('daily', selectedDate))
    } catch (error) {
      if (!axios.isAxiosError(error) || error.response?.status !== 404) {
        message.error(errorMessage(error, '读取日报草稿失败'))
      }
    } finally {
      setLoading(false)
    }
  }, [applyDraft])

  useEffect(() => {
    if (reportType === 'daily') {
      void loadDraft(businessDate.format('YYYY-MM-DD'))
    }
  }, [businessDate, loadDraft, reportType])

  const markChanged = useCallback(() => {
    const next = latestSequence.current + 1
    latestSequence.current = next
    setChangeSequence(next)
    setDirty(true)
    setSaveState('idle')
  }, [])

  useEffect(() => {
    if (!draft?.can_edit || !dirty || savingRef.current) return
    const expectedDraft = draft
    const sequence = changeSequence
    const timer = window.setTimeout(async () => {
      savingRef.current = true
      setSaveState('saving')
      try {
        const saved = await saveWorkLogDraft(expectedDraft.id, {
          version: expectedDraft.version,
          manual_values: manualRef.current,
          override_values: overrideRef.current,
        })
        setDraft(saved)
        if (sequence === latestSequence.current) {
          setDirty(false)
          setSaveState('saved')
        }
      } catch (error) {
        if (axios.isAxiosError(error) && error.response?.status === 409) {
          setSaveState('conflict')
          setDirty(false)
        } else {
          setSaveState('failed')
        }
      } finally {
        savingRef.current = false
      }
    }, 1000)
    return () => window.clearTimeout(timer)
  }, [changeSequence, dirty, draft])

  const setFieldValue = (field: WorkLogField, value: unknown) => {
    if (field.source === 'manual') {
      const next = { ...manualRef.current, [field.id]: value }
      manualRef.current = next
      setManualValues(next)
    } else {
      const next = { ...overrideRef.current, [field.id]: value }
      overrideRef.current = next
      setOverrideValues(next)
    }
    markChanged()
  }

  const restoreSystemValue = (fieldId: string) => {
    const next = { ...overrideRef.current }
    delete next[fieldId]
    overrideRef.current = next
    setOverrideValues(next)
    markChanged()
  }

  const effectiveValues = useMemo(() => {
    const system = draft?.system_snapshot.values || {}
    const tableInputs = { ...system, ...manualValues, ...overrideValues }
    return {
      ...system,
      ...manualValues,
      ...deriveWorkLogValues(tableInputs),
      ...overrideValues,
    }
  }, [draft, manualValues, overrideValues])

  if (user && !['admin', 'super_admin'].includes(user.role)) {
    return <Navigate to="/" replace />
  }

  const createDraft = async () => {
    setCreating(true)
    try {
      applyDraft(await createWorkLogDraft(
        'daily',
        businessDate.format('YYYY-MM-DD'),
      ))
      message.success('日报草稿已创建')
    } catch (error) {
      message.error(errorMessage(error, '创建日报草稿失败'))
    } finally {
      setCreating(false)
    }
  }

  const takeover = () => {
    if (!draft) return
    modal.confirm({
      title: '接管编辑权？',
      content: `接管后，“${draft.owner.username}”当前打开的页面将不能继续保存。`,
      okText: '确认接管',
      onOk: async () => {
        try {
          applyDraft(await takeoverWorkLogDraft(draft.id))
          message.success('已取得编辑权')
        } catch (error) {
          message.error(errorMessage(error, '接管失败'))
        }
      },
    })
  }

  const refreshSystemData = async () => {
    if (!draft || dirty || saveState === 'saving') return
    try {
      applyDraft(await refreshWorkLogDraft(draft.id, draft.version))
      message.success('系统数据已刷新，人工填写和覆盖值保持不变')
    } catch (error) {
      message.error(errorMessage(error, '刷新系统数据失败'))
    }
  }

  const runExport = async () => {
    if (!draft) return
    setExporting(true)
    try {
      const blob = await exportWorkLog(draft.id)
      saveBlob(
        blob,
        `${businessDate.format('MMDD')}日报滨湖新城派出所社区警务工作日志.pdf`,
      )
      message.success('PDF 工作日志已生成')
    } catch (error) {
      message.error(errorMessage(error, '生成 PDF 失败'))
    } finally {
      setExporting(false)
    }
  }

  const prepareExport = async () => {
    if (!draft) return
    if (dirty || saveState === 'saving') {
      message.warning('请等待当前内容保存完成后再导出')
      return
    }
    try {
      const result = await getWorkLogMissing(draft.id)
      if (result.count === 0) {
        await runExport()
        return
      }
      modal.confirm({
        title: `还有 ${result.count} 项未填写`,
        width: 620,
        content: (
          <div className="max-h-72 overflow-auto">
            {result.missing.map(item => (
              <div key={item.field_id} className="border-b border-slate-100 py-2 dark:border-slate-700">
                <div className="font-medium">{item.label}</div>
                <div className="text-xs text-slate-500">{item.reason}</div>
              </div>
            ))}
          </div>
        ),
        okText: '仍然导出 PDF',
        cancelText: '继续填写',
        onOk: runExport,
      })
    } catch (error) {
      message.error(errorMessage(error, '检查未填写内容失败'))
    }
  }

  const saveLabel = {
    idle: dirty ? '等待保存' : '未修改',
    saving: '保存中',
    saved: '已保存',
    failed: '保存失败，将在下次修改后重试',
    conflict: '保存冲突，请重新加载',
  }[saveState]

  return (
    <div>
      {contextHolder}
      <PageHeader
        title="工作日志生成"
        description="按原工作日志顺序填写全部内容，确认后导出固定版式 PDF"
        actions={(
          <Space wrap>
            <Button
              icon={<FolderOpenOutlined />}
              onClick={() => navigate('/work-log/drafts')}
            >
              草稿管理
            </Button>
            {draft && (
              <>
                <Tag color={
                  saveState === 'failed' || saveState === 'conflict'
                    ? 'error'
                    : saveState === 'saving'
                      ? 'processing'
                      : 'success'
                }>
                  {saveLabel}
                </Tag>
                {draft.can_edit ? (
                  <Button
                    icon={<ReloadOutlined />}
                    disabled={dirty || saveState === 'saving'}
                    onClick={refreshSystemData}
                  >
                    刷新系统数据
                  </Button>
                ) : (
                  <Button icon={<SwapOutlined />} onClick={takeover}>
                    接管编辑
                  </Button>
                )}
                <Button
                  type="primary"
                  icon={<CloudDownloadOutlined />}
                  loading={exporting}
                  onClick={prepareExport}
                >
                  导出 PDF
                </Button>
              </>
            )}
          </Space>
        )}
      />

      <Panel className="mb-4">
        <div className="flex flex-wrap items-end gap-4">
          <div>
            <div className="mb-2 text-sm font-medium">日志类型</div>
            <Segmented
              value={reportType}
              onChange={value => setReportType(String(value))}
              options={(schema?.report_types || [
                { value: 'daily', label: '日报', enabled: true },
                { value: 'weekly', label: '周报', enabled: false },
                { value: 'monthly', label: '月报', enabled: false },
              ]).map(item => ({
                value: item.value,
                label: item.enabled ? item.label : `${item.label} · 等待模板`,
                disabled: !item.enabled,
              }))}
            />
          </div>
          <div>
            <div className="mb-2 text-sm font-medium">日报日期</div>
            <DatePicker
              value={businessDate}
              allowClear={false}
              onChange={value => value && setBusinessDate(value)}
            />
          </div>
          <div className="text-sm text-slate-500">
            正文日期为 {businessDate.format('YYYY 年 M 月 D 日')}，落款为{' '}
            {businessDate.add(1, 'day').format('YYYY 年 M 月 D 日')}
          </div>
        </div>
      </Panel>

      {loading ? (
        <Panel><div className="py-12 text-center text-slate-500">正在读取草稿…</div></Panel>
      ) : !draft ? (
        <Panel>
          <Empty
            description={`${businessDate.format('M 月 D 日')} 尚未创建日报草稿`}
          >
            <Button type="primary" loading={creating} onClick={createDraft}>
              创建日报草稿
            </Button>
          </Empty>
        </Panel>
      ) : (
        <>
          {!draft.can_edit && (
            <Alert
              className="mb-4"
              showIcon
              type="info"
              message={`当前由 ${draft.owner.username} 编辑`}
              description="你可以查看和导出；需要修改时请主动接管编辑权。"
            />
          )}
          {draft.system_snapshot.legacy_v1 && (
            <Alert
              className="mb-4"
              showIcon
              type="info"
              message="旧版草稿已安全升级"
              description="旧版字段和内容已完整保存在草稿快照中，不会因新版表单而丢失。"
            />
          )}
          <div className="mb-4 grid gap-3 md:grid-cols-2">
            {Object.entries(draft.system_snapshot.sources).map(([key, source]) => (
              <Alert
                key={key}
                showIcon
                type={source.available
                  ? source.message ? 'warning' : 'success'
                  : 'warning'}
                message={`${source.label}：${source.available ? '已读取' : '无可用数据'}`}
                description={source.message || '系统表格已保存到本次草稿快照'}
              />
            ))}
          </div>
          <Panel className="mb-4">
            <div className="border-b border-slate-200 pb-5 text-center dark:border-slate-700">
              <div className="text-xl font-bold md:text-2xl">
                {schema?.document_title || '滨湖新城派出所社区警务工作日志'}
              </div>
              <div className="mt-2 text-sm text-slate-500">
                防控治理岗 · {businessDate.format('YYYY 年 M 月 D 日')}
              </div>
            </div>
            <div className="mx-auto max-w-[1500px]">
              {(schema?.sections || []).map(section => (
                <section
                  key={section.id}
                  className="border-b border-slate-200 py-6 last:border-b-0 dark:border-slate-700"
                >
                  <h1 className="mb-3 text-xl font-bold">{section.title}</h1>
                  {section.blocks.map((block, index) => (
                    <WorkLogBlockView
                      key={`${section.id}-${index}`}
                      block={block}
                      values={effectiveValues}
                      disabled={!draft.can_edit || saveState === 'conflict'}
                      overrides={overrideValues}
                      onChange={setFieldValue}
                      onRestore={restoreSystemValue}
                    />
                  ))}
                </section>
              ))}
            </div>
          </Panel>
        </>
      )}
    </div>
  )
}
