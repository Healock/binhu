import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  Alert,
  Button,
  Descriptions,
  Divider,
  Drawer,
  Empty,
  Form,
  Input,
  List,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Timeline,
  Upload,
  message,
} from 'antd'
import type { TableColumnsType } from 'antd'
import {
  DeleteOutlined,
  DownloadOutlined,
  EyeOutlined,
  FileExcelOutlined,
  InboxOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
  SelectOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import { ListToolbar, PageHeader, Panel } from '../components/ui'
import AppTable from '../components/AppTable'
import {
  workflowApi,
  formatUTCTime,
  type PendingPhotoRequest,
  type WorkOrderDetail,
  type WorkOrderSummary,
  type WorkflowType,
} from '../api/client'
import { useAuth } from '../context/AuthContext'
import useDebouncedValue from '../hooks/useDebouncedValue'
import { downloadBlob } from '../utils/fileDownload'

const STATUS_LABELS: Record<string, string> = {
  draft: '草稿', queued: '待领取', in_progress: '处理中', pending_requester: '待补充',
  approved: '已通过', completed: '已完成', rejected: '已驳回', cancelled: '已取消', withdrawn: '已撤回',
  pending: '待开始', returned: '已退回',
}

const EVENT_LABELS: Record<string, string> = {
  submit: '提交工单', claim: '领取工单', approve: '通过', reject: '驳回', return: '退回补充',
  complete: '完成', cancel: '取消', withdraw: '撤回', transfer: '转派', supplement: '补充材料',
  restore_queued: '恢复待领取',
  comment: '添加评论', attachment_upload: '上传附件', attachment_delete: '删除附件',
}

const PHOTO_DETAIL_LABELS: Record<string, string> = {
  subject_type: '对象类型', subject_id: '对象编号', subject_name: '对象姓名',
  identity_number: '身份证号', source_parser_type: '任务类型', source_row_key: '任务行号',
  requested_from: '开始时间', requested_to: '结束时间', request_reason: '申请理由',
  result_status: '调取结果', result_note: '处理说明',
  community_name: '任务社区', source_label: '数据来源', requester_name_snapshot: '原申请人',
  requested_at: '历史申请日期', external_origin: '历史来源渠道', external_sync_status: '历史来源状态',
  legacy_result_note: '历史备注', data_issue: '数据异常',
  batch_completed_at: '历史批次完成时间', tencent_physical_row: '历史来源位置',
  photo_sheet_batch_id: '历史批次编号', row_sync_status: '历史来源行状态',
}

const TERMINAL = new Set(['approved', 'completed', 'rejected', 'cancelled', 'withdrawn'])

function apiError(reason: any, fallback: string) {
  return reason?.response?.data?.detail || reason?.message || fallback
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

export default function WorkflowTickets({ mode = 'tickets' }: { mode?: 'tickets' | 'photo' }) {
  const { user, systemTimezone } = useAuth()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const requestedTicketId = Number(searchParams.get('ticket') || 0)
  const permissions = new Set(user?.permissions || [])
  const canCreate = permissions.has('workflow.ticket.create')
  const canHandle = permissions.has('workflow.ticket.handle') || permissions.has('workflow.ticket.manage')
  const canManage = permissions.has('workflow.ticket.manage')
  const canRestoreQueued = canManage || ['admin', 'super_admin'].includes(user?.role || '')
  const canViewAttachments = permissions.has('workflow.attachment.view')
  const canManageAttachments = canHandle
  const position = user?.member?.position || ''
  const canViewAll = canManage || ['基础管控', '中队长', '所队领导'].includes(position)
  const photoOnly = mode === 'photo'

  const [view, setView] = useState(photoOnly ? 'photo_pending' : 'created')
  const [rows, setRows] = useState<WorkOrderSummary[]>([])
  const [photoRows, setPhotoRows] = useState<PendingPhotoRequest[]>([])
  const [selectedPhotoIds, setSelectedPhotoIds] = useState<Array<string | number>>([])
  const [types, setTypes] = useState<WorkflowType[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [keyword, setKeyword] = useState('')
  const [keywordFlush, setKeywordFlush] = useState(0)
  const debouncedKeyword = useDebouncedValue(keyword.trim(), 350, keywordFlush)
  const [typeCode, setTypeCode] = useState('')
  const [photoSource, setPhotoSource] = useState('')
  const [photoCommunity, setPhotoCommunity] = useState('')
  const [attachmentStatus, setAttachmentStatus] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [detail, setDetail] = useState<WorkOrderDetail | null>(null)
  const [detailOpen, setDetailOpen] = useState(false)
  const [action, setAction] = useState('')
  const [actionOpen, setActionOpen] = useState(false)
  const [actionLoading, setActionLoading] = useState(false)
  const [photoClaiming, setPhotoClaiming] = useState(false)
  const [photoExporting, setPhotoExporting] = useState(false)
  const [createForm] = Form.useForm()
  const [actionForm] = Form.useForm()
  const listRequestId = useRef(0)
  const openedTicketId = useRef<number | null>(null)
  const selectedCreateTypeCode = Form.useWatch('type_code', createForm)
  const selectedCreateType = types.find(item => item.code === selectedCreateTypeCode)

  const typeName = useMemo(
    () => Object.fromEntries(types.map(item => [item.code, item.name])),
    [types],
  )

  const load = useCallback(async (nextPage = page, nextPageSize = pageSize) => {
    const requestId = ++listRequestId.current
    setLoading(true)
    setError('')
    try {
      if (view === 'photo_pending') {
        const result = await workflowApi.pendingPhotoRequests({
          keyword: debouncedKeyword,
          community: photoCommunity,
          source_label: photoSource,
          page: nextPage,
          page_size: nextPageSize,
        })
        if (requestId !== listRequestId.current) return
        setPhotoRows(result.data)
        setSelectedPhotoIds([])
        setTotal(result.total)
        setPage(nextPage)
        setPageSize(nextPageSize)
        return
      }
      const [result, typeResult] = await Promise.all([
        workflowApi.search({
          view, keyword: debouncedKeyword, type_code: typeCode, source_label: photoSource,
          attachment_status: attachmentStatus,
          page: nextPage, page_size: nextPageSize,
        }),
        types.length ? Promise.resolve({ data: types }) : workflowApi.types(),
      ])
      if (requestId !== listRequestId.current) return
      setRows(result.data)
      setTotal(result.total)
      setPage(nextPage)
      setPageSize(nextPageSize)
      setTypes(typeResult.data)
    } catch (reason) {
      if (requestId === listRequestId.current) setError(apiError(reason, '工单读取失败'))
    } finally {
      if (requestId === listRequestId.current) setLoading(false)
    }
  }, [attachmentStatus, debouncedKeyword, page, pageSize, photoCommunity, photoSource, typeCode, types, view])

  useEffect(() => {
    setView(photoOnly ? 'photo_pending' : 'created')
  }, [photoOnly])

  useEffect(() => { void load(1) }, [view, debouncedKeyword, typeCode, photoSource, photoCommunity, attachmentStatus])

  const refreshDetail = async (ticketId = detail?.id) => {
    if (!ticketId) return
    setDetail(await workflowApi.ticket(ticketId))
  }

  const openCreate = () => {
    createForm.resetFields()
    createForm.setFieldsValue({
      type_code: types.find(item => item.enabled)?.code || 'photo_request',
      priority: 'normal',
    })
    setCreateOpen(true)
  }

  const create = async () => {
    try {
      const values = await createForm.validateFields()
      const selectedType = types.find(item => item.code === values.type_code)
      const formData = values.type_code === 'leave_request'
        ? {
            leave_type: values.leave_type || 'temporary_leave',
            start_date: values.start_date,
            end_date: values.end_date,
            reason: values.reason || '',
            affects_weekend_duty: Boolean(values.affects_weekend_duty),
          }
        : values.type_code === 'photo_request'
              ? {
                  subject_type: values.subject_type || 'task',
                  subject_id: values.subject_id || '',
                  subject_name: values.subject_name || '',
                  identity_number: values.identity_number || '',
                  request_reason: values.reason || '',
            requested_from: values.requested_from || null,
            requested_to: values.requested_to || null,
          }
        : values.custom_fields || {}
      await workflowApi.createTicket({
        type_code: values.type_code,
        title: values.title,
        description: values.description || '',
        priority: values.priority,
        form_data: formData,
        links: [],
      })
      message.success('工单已提交')
      setCreateOpen(false)
      await load(1)
    } catch (reason: any) {
      if (!reason?.errorFields) message.error(apiError(reason, '工单提交失败'))
    }
  }

  const openDetail = useCallback(async (row: Pick<WorkOrderSummary, 'id'>) => {
    try {
      setDetail(await workflowApi.ticket(row.id))
      setDetailOpen(true)
    } catch (reason) {
      message.error(apiError(reason, '工单详情读取失败'))
    }
  }, [])

  useEffect(() => {
    if (!Number.isInteger(requestedTicketId) || requestedTicketId <= 0) return
    if (openedTicketId.current === requestedTicketId) return
    openedTicketId.current = requestedTicketId
    void openDetail({ id: requestedTicketId })
  }, [openDetail, requestedTicketId])

  const closeDetail = () => {
    setDetailOpen(false)
    if (!searchParams.has('ticket')) return
    const next = new URLSearchParams(searchParams)
    next.delete('ticket')
    setSearchParams(next, { replace: true })
    openedTicketId.current = null
  }

  const claim = async (row: WorkOrderSummary) => {
    try {
      await workflowApi.claim(row.id, row.version_no)
      message.success('工单已领取')
      await load()
      if (detail?.id === row.id) await refreshDetail(row.id)
    } catch (reason) {
      message.error(apiError(reason, '领取失败'))
    }
  }

  const claimPhotoRequests = async (claimAll: boolean) => {
    const ticketIds = selectedPhotoIds.map(Number).filter(Number.isInteger)
    if (!claimAll && ticketIds.length === 0) return
    setPhotoClaiming(true)
    try {
      const result = await workflowApi.batchClaimPhotoRequests({
        claim_all: claimAll,
        ticket_ids: claimAll ? [] : ticketIds,
      })
      const skipped = result.skipped_ids.length
      message.success(
        skipped
          ? `已领取 ${result.claimed_count} 张工单，${skipped} 张已被他人处理或状态已变化`
          : `已领取 ${result.claimed_count} 张照片工单`,
      )
      await load(1)
    } catch (reason) {
      message.error(apiError(reason, '批量领取失败'))
    } finally {
      setPhotoClaiming(false)
    }
  }

  const exportPhotoRequests = async () => {
    setPhotoExporting(true)
    try {
      const blob = await workflowApi.exportPendingPhotoRequests({
        keyword,
        community: photoCommunity,
        source_label: photoSource,
      })
      const stamp = new Date().toISOString().slice(0, 10).replaceAll('-', '')
      const saved = await downloadBlob(blob, `未调照片-${stamp}.xlsx`)
      if (saved) message.success('未调照片清单已导出')
    } catch (reason) {
      message.error(apiError(reason, '导出失败'))
    } finally {
      setPhotoExporting(false)
    }
  }

  const openAction = (nextAction: string) => {
    actionForm.resetFields()
    actionForm.setFieldsValue({
      result_status: detail?.type_code === 'photo_request' ? 'found' : undefined,
      target_queue: detail?.current_queue || '基础管控',
    })
    setAction(nextAction)
    setActionOpen(true)
  }

  const submitAction = async () => {
    if (!detail) return
    try {
      const values = await actionForm.validateFields()
      setActionLoading(true)
      if (action === 'transfer') {
        await workflowApi.transfer(detail.id, {
          expected_version: detail.version_no,
          target_queue: values.target_queue,
          target_user_id: null,
          reason: values.note,
        })
      } else if (action === 'supplement') {
        await workflowApi.supplement(detail.id, {
          expected_version: detail.version_no,
          note: values.note || '',
          form_data: { supplement_note: values.note || '' },
        })
      } else if (action === 'restore_queued') {
        await workflowApi.restoreQueued(detail.id, {
          expected_version: detail.version_no,
          reason: values.note,
        })
      } else if (action === 'withdraw') {
        await workflowApi.withdraw(detail.id, {
          expected_version: detail.version_no,
          reason: values.note || '',
        })
      } else if (action === 'comment') {
        await workflowApi.comments(detail.id, values.note, detail.version_no)
      } else {
        await workflowApi.decide(detail.id, {
          action,
          note: values.note || '',
          result_status: values.result_status || '',
          expected_version: detail.version_no,
        })
      }
      message.success(action === 'comment' ? '评论已添加' : '工单状态已更新')
      setActionOpen(false)
      await refreshDetail(detail.id)
      await load()
    } catch (reason: any) {
      if (!reason?.errorFields) message.error(apiError(reason, '操作失败'))
    } finally {
      setActionLoading(false)
    }
  }

  const upload = async (file: File) => {
    if (!detail) return false
    try {
      await workflowApi.uploadAttachment(detail.id, file, detail.version_no)
      message.success('附件已上传')
      await refreshDetail(detail.id)
    } catch (reason) {
      message.error(apiError(reason, '附件上传失败'))
    }
    return false
  }

  const deleteAttachment = async (fileId: string) => {
    if (!detail) return
    try {
      await workflowApi.deleteAttachment(detail.id, fileId, detail.version_no)
      message.success('附件已删除，操作记录仍会保留')
      await refreshDetail(detail.id)
    } catch (reason) {
      message.error(apiError(reason, '附件删除失败'))
    }
  }

  const columns: TableColumnsType<WorkOrderSummary> = [
    { title: '工单编号', dataIndex: 'ticket_no', width: 190 },
    { title: '类型', dataIndex: 'type_code', width: 130, render: value => typeName[value] || value },
    { title: '标题', dataIndex: 'title', ellipsis: true },
    {
      title: '状态', dataIndex: 'status', width: 110,
      render: value => <Tag color={['completed', 'approved'].includes(value) ? 'green' : value === 'rejected' ? 'red' : 'blue'}>{STATUS_LABELS[value] || value}</Tag>,
    },
    { title: '队列', dataIndex: 'current_queue', width: 120, render: value => value || '—' },
    {
      title: '截止时间', dataIndex: 'due_at', width: 180,
      render: (value, row) => <span className={row.overdue ? 'text-red-600' : ''}>{value ? formatUTCTime(value, systemTimezone) : '未设置'}</span>,
    },
    {
      title: '操作', width: 150,
      render: (_, row) => (
        <Space>
          <Button size="small" onClick={() => void openDetail(row)}>详情</Button>
          {row.status === 'queued' && canHandle && <Button size="small" type="primary" onClick={() => void claim(row)}>领取</Button>}
        </Space>
      ),
    },
  ]

  const photoColumns: TableColumnsType<PendingPhotoRequest> = [
    { title: '姓名', dataIndex: 'subject_name', width: 120, fixed: 'left' },
    {
      title: '身份证号', dataIndex: 'identity_number', width: 200,
      render: value => <span className="font-mono">{value || '—'}</span>,
    },
    { title: '社区', dataIndex: 'community_name', width: 140, render: value => value || '—' },
    { title: '申请人员', dataIndex: 'requester_name', width: 120, render: value => value || '—' },
    {
      title: '申请时间', dataIndex: 'requested_at', width: 170,
      render: value => value ? formatUTCTime(value, systemTimezone) : '—',
    },
    { title: '数据来源', dataIndex: 'source_label', width: 180, ellipsis: true, render: value => value || '—' },
    { title: '工单编号', dataIndex: 'ticket_no', width: 210 },
    {
      title: '状态', dataIndex: 'status', width: 110,
      render: value => <Tag color={value === 'in_progress' ? 'processing' : 'default'}>{STATUS_LABELS[value] || value}</Tag>,
    },
    {
      title: '截止时间', dataIndex: 'due_at', width: 170,
      render: (value, row) => <span className={row.overdue ? 'text-red-600' : ''}>{value ? formatUTCTime(value, systemTimezone) : '未设置'}</span>,
    },
  ]

  const requesterOwnsDetail = Boolean(detail && user?.id === detail.requester_user_id)
  const canProcessDetail = Boolean(detail && canHandle && ['queued', 'in_progress'].includes(detail.status))

  return (
    <div className="workflow-tickets-page app-page">
      <PageHeader
        title={photoOnly ? '调照片' : '工单中心'}
        description={photoOnly
          ? '基础管控在这里集中领取、导出和处理照片调取任务，照片 ZIP 仍从数据上传中心导入。'
          : '请假、照片调取等通用流程统一在这里发起和查看。版本冲突会要求刷新，不会静默覆盖他人的操作。'}
      />
      {error && <Alert type="error" showIcon message={error} />}
      <Panel className="workflow-tickets-panel">
        <div className="workflow-tickets-panel__content">
          {!photoOnly && (
            <Tabs
              activeKey={view}
              onChange={value => { setView(value); setPage(1) }}
              items={[
                { key: 'created', label: '我的发起' },
                { key: 'claimable', label: '待领取' },
                { key: 'handling', label: '处理中' },
                { key: 'supplement', label: '待补充' },
                { key: 'processed', label: '已处理' },
                ...(canViewAll ? [{ key: 'all', label: '全部工单' }] : []),
              ]}
            />
          )}
          {view === 'photo_pending' ? (
            <div className="workflow-photo-workbench">
              <ListToolbar
                notice={<Alert type="info" showIcon message="三步完成：领取全部待领取工单，导出清单集中调照片，再到数据上传中心上传照片 ZIP。" />}
                filters={<>
                  <Input
                    allowClear
                    className="workflow-photo-search"
                    size="large"
                    prefix={<SearchOutlined className="workflow-photo-search__icon" />}
                    placeholder="搜索姓名、身份证号或工单编号"
                    value={keyword}
                    onChange={event => setKeyword(event.target.value)}
                    onPressEnter={() => setKeywordFlush(current => current + 1)}
                  />
                  <Input
                    allowClear
                    className="workflow-photo-toolbar__field"
                    size="large"
                    placeholder="社区"
                    value={photoCommunity}
                    onChange={event => setPhotoCommunity(event.target.value)}
                  />
                  <Input
                    allowClear
                    className="workflow-photo-toolbar__field"
                    size="large"
                    placeholder="数据来源"
                    value={photoSource}
                    onChange={event => setPhotoSource(event.target.value)}
                  />
                </>}
                meta={<><span>当前筛选 {total} 张</span><span>已选择 {selectedPhotoIds.length} 张</span></>}
                actions={<>
                  <Popconfirm
                    title="领取全部待领取的照片工单？"
                    description="已被其他人领取的工单会自动跳过。领取后仍会保留在当前表格中。"
                    okText="全部领取"
                    cancelText="取消"
                    onConfirm={() => claimPhotoRequests(true)}
                  >
                    <Button type="primary" icon={<SelectOutlined />} loading={photoClaiming}>
                      领取全部待领取
                    </Button>
                  </Popconfirm>
                  <Button
                    icon={<SelectOutlined />}
                    disabled={selectedPhotoIds.length === 0}
                    loading={photoClaiming}
                    onClick={() => void claimPhotoRequests(false)}
                  >
                    领取所选
                  </Button>
                  <Button
                    icon={<FileExcelOutlined />}
                    loading={photoExporting}
                    onClick={() => void exportPhotoRequests()}
                  >
                    导出 XLSX
                  </Button>
                  <Button
                    icon={<UploadOutlined />}
                    onClick={() => navigate('/data-upload')}
                  >
                    前往数据上传中心
                  </Button>
                  <Button icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button>
                </>}
              />
              <AppTable
                rowKey="id"
                loading={loading}
                columns={photoColumns}
                dataSource={photoRows}
                rowSelection={{
                  selectedRowKeys: selectedPhotoIds,
                  onChange: keys => setSelectedPhotoIds(keys),
                  getCheckboxProps: row => ({ disabled: row.status !== 'queued' }),
                }}
                pagination={{
                  current: page,
                  pageSize,
                  total,
                  showSizeChanger: true,
                  pageSizeOptions: [20, 50, 100, 200],
                  onChange: (next, size) => void load(next, size),
                }}
                scroll={{ x: 1420 }}
              />
            </div>
          ) : (
            <div className="workflow-ticket-list">
              <ListToolbar
                filters={<>
                <Input
                  allowClear
                  className="min-w-64"
                  prefix={<SearchOutlined />}
                  placeholder="搜索工单编号或标题"
                  value={keyword}
                  onChange={event => setKeyword(event.target.value)}
                  onPressEnter={() => setKeywordFlush(current => current + 1)}
                />
                <Select
                  allowClear
                  className="min-w-44"
                  placeholder="全部工单类型"
                  value={typeCode || undefined}
                  onChange={value => setTypeCode(value || '')}
                  options={types.map(item => ({ value: item.code, label: item.name }))}
                />
                <Input
                  allowClear
                  className="max-w-48"
                  placeholder="照片数据来源"
                  value={photoSource}
                  onChange={event => setPhotoSource(event.target.value)}
                />
                <Select
                  allowClear
                  className="min-w-36"
                  placeholder="附件状态"
                  value={attachmentStatus || undefined}
                  onChange={value => setAttachmentStatus(value || '')}
                  options={[{ value: 'with', label: '有平台附件' }, { value: 'without', label: '无平台附件' }]}
                />
                </>}
                meta={<span>当前筛选 {total} 张工单</span>}
                actions={<>
                  <Button icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button>
                  {canCreate && <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建工单</Button>}
                </>}
              />
              <AppTable
                rowKey="id"
                loading={loading}
                columns={columns}
                dataSource={rows}
                pagination={{
                  current: page,
                  pageSize,
                  total,
                  showSizeChanger: true,
                  onChange: (next, size) => void load(next, size),
                }}
                scroll={{ x: 1100 }}
              />
            </div>
          )}
        </div>
      </Panel>

      <Drawer
        open={detailOpen}
        width="min(96vw, 820px)"
        title={detail?.ticket_no || '工单详情'}
        onClose={closeDetail}
        extra={<Button icon={<ReloadOutlined />} onClick={() => void refreshDetail()}>刷新</Button>}
      >
        {detail && (
          <div className="workflow-ticket-detail">
            <Descriptions
              bordered
              size="small"
              column={1}
              items={[
                { key: 'title', label: '标题', children: detail.title },
                { key: 'type', label: '类型', children: typeName[detail.type_code] || detail.type_code },
                { key: 'status', label: '状态', children: STATUS_LABELS[detail.status] || detail.status },
                { key: 'queue', label: '当前队列', children: detail.current_queue || '—' },
                { key: 'due', label: '截止时间', children: detail.due_at ? formatUTCTime(detail.due_at, systemTimezone) : '未设置' },
                { key: 'description', label: '说明', children: detail.description || '无' },
              ]}
            />

            {detail.type_detail && (
              <section className="workflow-ticket-detail__section">
                <h3 className="workflow-ticket-detail__heading">
                  {detail.type_code === 'leave_request' ? '请假信息' : '照片调取信息'}
                </h3>
                <Descriptions
                  bordered
                  size="small"
                  column={1}
                  items={Object.entries(detail.type_detail).map(([key, value]) => ({
                    key,
                    label: detail.type_code === 'photo_request' ? (PHOTO_DETAIL_LABELS[key] || key) : key,
                    children: value === null || value === ''
                      ? '—'
                      : ['requested_at', 'batch_completed_at'].includes(key)
                        ? formatUTCTime(String(value), systemTimezone)
                        : String(value),
                  }))}
                />
              </section>
            )}

            <Space wrap className="workflow-ticket-detail__actions">
              {detail.status === 'queued' && canHandle && <Button type="primary" onClick={() => void claim(detail)}>领取</Button>}
              {canProcessDetail && <Button onClick={() => openAction('approve')}>通过</Button>}
              {canProcessDetail && <Button onClick={() => openAction('complete')}>完成</Button>}
              {canProcessDetail && <Button onClick={() => openAction('return')}>退回补充</Button>}
              {canProcessDetail && <Button danger onClick={() => openAction('reject')}>驳回</Button>}
              {canProcessDetail && <Button onClick={() => openAction('transfer')}>转派</Button>}
              {requesterOwnsDetail && detail.status === 'pending_requester' && <Button type="primary" onClick={() => openAction('supplement')}>补充材料</Button>}
              {canRestoreQueued && detail.status === 'pending_requester' && (
                <Button type="primary" onClick={() => openAction('restore_queued')}>恢复待领取</Button>
              )}
              {requesterOwnsDetail && !TERMINAL.has(detail.status) && <Button danger onClick={() => openAction('withdraw')}>撤回</Button>}
              <Button onClick={() => openAction('comment')}>添加评论</Button>
            </Space>

            <section className="workflow-ticket-detail__section">
              <Divider className="workflow-ticket-detail__divider" orientation="left">流程节点</Divider>
              <Timeline
                items={(detail.steps || []).map(step => ({
                  color: ['approved', 'completed'].includes(step.status) ? 'green' : step.status === 'rejected' ? 'red' : step.status === 'in_progress' ? 'blue' : 'gray',
                  children: (
                    <div>
                      <div className="font-medium">{step.step_order}. {step.name}</div>
                      <div className="text-sm text-[var(--app-text-secondary)]">
                        {STATUS_LABELS[step.status] || step.status} · {step.queue || '未配置队列'}
                        {step.decision_note ? ` · ${step.decision_note}` : ''}
                      </div>
                    </div>
                  ),
                }))}
              />
            </section>

            <section className="workflow-ticket-detail__section">
              <Divider className="workflow-ticket-detail__divider" orientation="left">附件</Divider>
              {canManageAttachments && (
                <Upload beforeUpload={upload} showUploadList={false} accept=".jpg,.jpeg,.png,.webp,.heic,.pdf">
                  <Button icon={<InboxOutlined />}>上传附件</Button>
                </Upload>
              )}
              {canViewAttachments ? (
                <List
                  locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无附件" /> }}
                  dataSource={(detail.attachments || []).filter(item => !item.deleted_at)}
                  renderItem={item => (
                    <List.Item
                      actions={[
                        ...(item.mime_type.startsWith('image/') ? [
                          <a
                            key="preview"
                            href={workflowApi.attachmentUrl(detail.id, item.file_id, true)}
                            target="_blank"
                            rel="noreferrer"
                          >
                            <EyeOutlined /> 预览
                          </a>,
                        ] : []),
                        <a key="download" href={workflowApi.attachmentUrl(detail.id, item.file_id)}>
                          <DownloadOutlined /> 下载
                        </a>,
                        ...(canManageAttachments ? [
                          <Popconfirm key="delete" title="删除这个附件？" onConfirm={() => void deleteAttachment(item.file_id)}>
                            <Button type="link" danger icon={<DeleteOutlined />}>删除</Button>
                          </Popconfirm>,
                        ] : []),
                      ]}
                    >
                      <List.Item.Meta title={item.original_name} description={`${formatBytes(item.size_bytes)} · ${item.mime_type}`} />
                    </List.Item>
                  )}
                />
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前账号无附件查看权限" />
              )}
            </section>

            <section className="workflow-ticket-detail__section">
              <Divider className="workflow-ticket-detail__divider" orientation="left">评论</Divider>
              <List
                locale={{ emptyText: '暂无评论' }}
                dataSource={detail.comments || []}
                renderItem={item => <List.Item><List.Item.Meta title={`账号 #${item.user_id}`} description={<><div>{item.content}</div><div>{formatUTCTime(item.created_at, systemTimezone)}</div></>} /></List.Item>}
              />
            </section>

            <section className="workflow-ticket-detail__section">
              <Divider className="workflow-ticket-detail__divider" orientation="left">事件记录</Divider>
              <Table
                size="small"
                pagination={false}
                dataSource={detail.events || []}
                rowKey={(row: any, index) => `${row.event_type}-${row.created_at}-${index}`}
                columns={[
                  { title: '事件', dataIndex: 'event_type', render: value => EVENT_LABELS[value] || value },
                  { title: '状态', render: (_: any, row: any) => `${STATUS_LABELS[row.from_status] || row.from_status || '—'} → ${STATUS_LABELS[row.to_status] || row.to_status || '—'}` },
                  { title: '时间', dataIndex: 'created_at', width: 190, render: value => formatUTCTime(value, systemTimezone) },
                ]}
              />
            </section>
          </div>
        )}
      </Drawer>

      <Modal
        open={createOpen}
        title="新建工单"
        okText="提交"
        cancelText="取消"
        onOk={() => void create()}
        onCancel={() => setCreateOpen(false)}
      >
        <Form form={createForm} layout="vertical">
          <Form.Item name="type_code" label="工单类型" rules={[{ required: true }]}>
            <Select options={types.filter(item => item.enabled).map(item => ({ value: item.code, label: item.name }))} />
          </Form.Item>
          <Form.Item name="title" label="标题" rules={[{ required: true }]}><Input maxLength={200} /></Form.Item>
          <Form.Item name="description" label="说明"><Input.TextArea rows={3} maxLength={5000} /></Form.Item>
          <Form.Item name="priority" label="优先级">
            <Select options={[{ value: 'normal', label: '普通' }, { value: 'high', label: '重要' }, { value: 'urgent', label: '紧急' }]} />
          </Form.Item>
          <Form.Item noStyle shouldUpdate>
            {({ getFieldValue }) => getFieldValue('type_code') === 'leave_request' ? (
              <>
                <Form.Item name="leave_type" label="请假类型" rules={[{ required: true }]}>
                  <Select options={[{ value: 'temporary_leave', label: '事假' }, { value: 'sick_leave', label: '病假' }, { value: 'annual_leave', label: '年假' }]} />
                </Form.Item>
                <Form.Item name="start_date" label="开始日期" rules={[{ required: true }]}><Input placeholder="YYYY-MM-DD" /></Form.Item>
                <Form.Item name="end_date" label="结束日期" rules={[{ required: true }]}><Input placeholder="YYYY-MM-DD" /></Form.Item>
                <Form.Item name="reason" label="原因"><Input.TextArea maxLength={1000} /></Form.Item>
              </>
            ) : getFieldValue('type_code') === 'photo_request' ? (
              <>
                <Form.Item name="subject_type" label="对象类型"><Select options={[{ value: 'task', label: '指令任务' }, { value: 'person', label: '人员' }, { value: 'other', label: '其他' }]} /></Form.Item>
                <Form.Item name="subject_id" label="对象编号"><Input maxLength={190} /></Form.Item>
                <Form.Item name="subject_name" label="对象姓名" rules={[{ required: true, message: '请填写对象姓名' }]}><Input maxLength={100} /></Form.Item>
                <Form.Item name="identity_number" label="身份证号" rules={[{ required: true, message: '请填写身份证号' }]}><Input maxLength={50} /></Form.Item>
                <Form.Item name="reason" label="申请理由" rules={[{ required: true }]}><Input.TextArea rows={3} maxLength={1000} /></Form.Item>
              </>
            ) : (
              <>
                {(selectedCreateType?.form_schema?.fields || []).map((field: any) => (
                  <Form.Item
                    key={field.name}
                    name={['custom_fields', field.name]}
                    label={field.label || field.name}
                    rules={[{ required: Boolean(field.required), message: `请填写${field.label || field.name}` }]}
                  >
                    {field.type === 'textarea' ? <Input.TextArea rows={3} />
                      : field.type === 'select' ? <Select options={(field.options || []).map((option: string) => ({ value: option, label: option }))} />
                      : <Input type={field.type === 'number' ? 'number' : 'text'} />}
                  </Form.Item>
                ))}
              </>
            )}
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={actionOpen}
        title={{ transfer: '转派工单', supplement: '补充材料', restore_queued: '恢复待领取', withdraw: '撤回工单', comment: '添加评论', approve: '通过工单', complete: '完成工单', return: '退回补充', reject: '驳回工单' }[action] || '处理工单'}
        okText="确认"
        cancelText="取消"
        confirmLoading={actionLoading}
        onOk={() => void submitAction()}
        onCancel={() => setActionOpen(false)}
      >
        {action === 'restore_queued' && (
          <Alert
            className="mb-4"
            type="warning"
            showIcon
            message="确认恢复为待领取"
            description="系统将清空当前处理人并重新放回原岗位队列，原退回记录和本次恢复记录都会保留。"
          />
        )}
        <Form form={actionForm} layout="vertical">
          {action === 'transfer' && (
            <Form.Item name="target_queue" label="目标岗位队列" rules={[{ required: true }]}>
              <Select options={['基础管控', '中队长', '组长', '组员', '社区民警', '所队领导'].map(value => ({ value, label: value }))} />
            </Form.Item>
          )}
          {detail?.type_code === 'photo_request' && ['approve', 'complete'].includes(action) && (
            <Form.Item name="result_status" label="调取结果" rules={[{ required: true }]}>
              <Select options={[{ value: 'found', label: '已找到照片' }, { value: 'not_found', label: '未找到照片' }]} />
            </Form.Item>
          )}
          <Form.Item
            name="note"
            label={action === 'comment' ? '评论内容' : action === 'supplement' ? '补充说明' : action === 'restore_queued' ? '恢复原因' : '处理说明'}
            rules={[{ required: ['transfer', 'return', 'reject', 'comment', 'supplement', 'restore_queued'].includes(action), message: '请填写说明' }]}
          >
            <Input.TextArea rows={4} maxLength={action === 'restore_queued' ? 500 : 2000} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
