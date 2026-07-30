import {
  Alert,
  Button,
  Collapse,
  DatePicker,
  Empty,
  Input,
  InputNumber,
  Modal,
  Segmented,
  Space,
  Table,
  Tag,
  message,
} from 'antd'
import type { TableColumnsType } from 'antd'
import {
  CloudDownloadOutlined,
  DeleteOutlined,
  PlusOutlined,
  ReloadOutlined,
  SwapOutlined,
} from '@ant-design/icons'
import axios from 'axios'
import dayjs, { type Dayjs } from 'dayjs'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Navigate } from 'react-router-dom'
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
  WorkLogDraft,
  WorkLogField,
  WorkLogSchema,
} from '../types'

type Values = Record<string, unknown>
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

function ValueField({
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
  if (field.type === 'table') {
    const rows = Array.isArray(value)
      ? value as Array<Record<string, string>>
      : []
    const columns: TableColumnsType<Record<string, string>> = [
      ...(field.columns || []).map(column => ({
        title: column.label,
        dataIndex: column.key,
        render: (_: unknown, row: Record<string, string>, index: number) => (
          <Input
            value={row[column.key]}
            disabled={disabled}
            onChange={(event) => {
              const next = rows.map((item, rowIndex) => (
                rowIndex === index
                  ? { ...item, [column.key]: event.target.value }
                  : item
              ))
              onChange(next)
            }}
          />
        ),
      })),
      {
        title: '操作',
        width: 76,
        render: (_: unknown, __: Record<string, string>, index: number) => (
          <Button
            type="text"
            danger
            icon={<DeleteOutlined />}
            disabled={disabled}
            aria-label={`删除第 ${index + 1} 行`}
            onClick={() => onChange(rows.filter((_, rowIndex) => rowIndex !== index))}
          />
        ),
      },
    ]
    return (
      <div className="space-y-2">
        <Table
          rowKey={(_, index) => String(index)}
          size="small"
          pagination={false}
          dataSource={rows}
          columns={columns}
          scroll={{ x: 640 }}
          locale={{ emptyText: '暂无明细，可按需要添加' }}
        />
        {!disabled && (
          <Button
            icon={<PlusOutlined />}
            onClick={() => {
              const empty = Object.fromEntries(
                (field.columns || []).map(column => [column.key, '']),
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

  const input = field.type === 'textarea'
    ? (
        <Input.TextArea
          value={value == null ? '' : String(value)}
          disabled={disabled}
          autoSize={{ minRows: 3, maxRows: 8 }}
          onChange={event => onChange(event.target.value)}
        />
      )
    : field.type === 'text'
      ? (
          <Input
            value={value == null ? '' : String(value)}
            disabled={disabled}
            onChange={event => onChange(event.target.value)}
          />
        )
      : (
          <InputNumber
            value={value == null || value === '' ? null : Number(value)}
            disabled={disabled}
            min={0}
            precision={field.type === 'number' ? 0 : 1}
            suffix={field.type === 'percent' ? '%' : undefined}
            className="w-full"
            onChange={onChange}
          />
        )

  return (
    <div>
      {input}
      {field.source === 'system' && (
        <div className="mt-1 flex min-h-6 items-center gap-2">
          <Tag color={overridden ? 'orange' : 'blue'}>
            {overridden ? '人工修改' : '系统数据'}
          </Tag>
          {overridden && !disabled && (
            <Button type="link" size="small" onClick={onRestore}>
              恢复系统值
            </Button>
          )}
        </div>
      )}
    </div>
  )
}

export default function WorkLog() {
  const { user } = useAuth()
  const [schema, setSchema] = useState<WorkLogSchema | null>(null)
  const [reportType, setReportType] = useState('daily')
  const [businessDate, setBusinessDate] = useState<Dayjs>(dayjs())
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
    if (field.source === 'system') {
      const next = { ...overrideRef.current, [field.id]: value }
      overrideRef.current = next
      setOverrideValues(next)
    } else {
      const next = { ...manualRef.current, [field.id]: value }
      manualRef.current = next
      setManualValues(next)
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

  const effectiveValues = useMemo(() => ({
    ...(draft?.system_snapshot.values || {}),
    ...manualValues,
    ...overrideValues,
  }), [draft, manualValues, overrideValues])

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
        `${businessDate.format('MMDD')}日报滨湖新城派出所社区警务工作日志.docx`,
      )
      message.success('工作日志已生成')
    } catch (error) {
      message.error(errorMessage(error, '生成工作日志失败'))
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
              <div key={item.field_id} className="border-b border-slate-100 py-2">
                <div className="font-medium">{item.label}</div>
                <div className="text-xs text-slate-500">{item.reason}</div>
              </div>
            ))}
          </div>
        ),
        okText: '仍然导出',
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
        description="选择日期，补充系统无法自动获得的内容，再导出 Word 文档"
        actions={draft && (
          <Space wrap>
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
              导出 DOCX
            </Button>
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
            <div className="mb-2 text-sm font-medium">业务日期</div>
            <DatePicker
              value={businessDate}
              allowClear={false}
              onChange={value => value && setBusinessDate(value)}
            />
          </div>
          <div className="text-sm text-slate-500">
            查询 {businessDate.format('M 月 D 日')} 数据，文件落款为{' '}
            {businessDate.add(1, 'day').format('M 月 D 日')}
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
          <div className="mb-4 grid gap-3 md:grid-cols-3">
            {Object.entries(draft.system_snapshot.sources).map(([key, source]) => (
              <Alert
                key={key}
                showIcon
                type={source.available ? (source.message ? 'warning' : 'success') : 'warning'}
                message={`${source.label}：${source.available ? '已读取' : '无可用数据'}`}
                description={source.message || '系统值已保存到本次草稿快照'}
              />
            ))}
          </div>
          <Collapse
            defaultActiveKey={schema?.sections.map(section => section.id)}
            items={(schema?.sections || []).map(section => ({
              key: section.id,
              label: (
                <div>
                  <div className="font-semibold">{section.title}</div>
                  {section.description && (
                    <div className="text-xs font-normal text-slate-500">
                      {section.description}
                    </div>
                  )}
                </div>
              ),
              children: (
                <div className="grid gap-4 md:grid-cols-2">
                  {section.fields.map(field => (
                    <div
                      key={field.id}
                      className={field.type === 'table' || field.type === 'textarea'
                        ? 'md:col-span-2'
                        : ''}
                    >
                      <div className="mb-1.5 flex items-center gap-2 text-sm font-medium">
                        <span>{field.label}</span>
                        {field.required && <span className="text-red-500">*</span>}
                      </div>
                      <ValueField
                        field={field}
                        value={effectiveValues[field.id]}
                        disabled={!draft.can_edit || saveState === 'conflict'}
                        overridden={Object.prototype.hasOwnProperty.call(
                          overrideValues,
                          field.id,
                        )}
                        onChange={value => setFieldValue(field, value)}
                        onRestore={() => restoreSystemValue(field.id)}
                      />
                    </div>
                  ))}
                </div>
              ),
            }))}
          />
        </>
      )}
    </div>
  )
}
