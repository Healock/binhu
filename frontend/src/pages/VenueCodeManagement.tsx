import { useEffect, useState } from 'react'
import { Alert, Button, DatePicker, Form, Input, Modal, Popconfirm, Progress, Select, Space, Table, Tag, Upload, message, Tabs } from 'antd'
import { DeleteOutlined, DownloadOutlined, EyeOutlined, PlusOutlined, QrcodeOutlined, ReloadOutlined } from '@ant-design/icons'
import {
  apiErrorMessage,
  formatUTCTime,
  createVenueCode,
  deleteVenueCode,
  deleteVenueVisit,
  exportVenueVisitsZip,
  getVenueVisitPhotoUrl,
  getVenueCloudStatus,
  pullVenueCloudNow,
  getVenueCodeQr,
  listVenueCodes,
  listVenueVisits,
  rotateVenueCodeToken,
  updateVenueCode,
  type VenueCloudStatus,
  type VenueCodeInput,
  type VenueCodeItem,
  type VenueVisitItem,
  createDrinkingFormCode,
  exportDrinkingReportPdf,
  getDrinkingFormCode,
  getDrinkingFormQr,
  listDrinkingReports,
  rotateDrinkingFormCode,
  updateDrinkingFormStatus,
  type DrinkingReport,
  type DrinkingFormCode,
  getDrinkingReport,
} from '../api/client'
import { downloadBlob } from '../utils/fileDownload'
import { PageHeader, Panel } from '../components/ui'
import { useAuth } from '../context/AuthContext'
import AuthenticatedImage from '../components/AuthenticatedImage'
import { resolveRuntimeApiUrl } from '../utils/apiEnvironment'
import { readVenueErrorPayload, venueRegistrationErrorMessage } from '../utils/venueRegistration'

const emptyVenue: VenueCodeInput = {
  name: '',
  venue_type: '',
  address: '',
  community_id: null,
  community_name: '',
  status: 'active',
}

function cloudState(row: VenueCodeItem) {
  if (row.cloud_sync_status === 'error') return <Tag color="error">同步失败</Tag>
  if (row.cloud_sync_status === 'pending') {
    if (row.status === 'deleted') return <Tag color="warning">云端删除待确认</Tag>
    if (row.status === 'inactive') return <Tag color="warning">云端停用待确认</Tag>
    if (row.pending_token_version != null) return <Tag color="processing">新二维码待确认</Tag>
    return <Tag color="processing">云端同步中</Tag>
  }
  if (row.cloud_sync_status === 'confirmed') return <Tag color="success">云端已确认</Tag>
  return <Tag>仅本地</Tag>
}

function formatTime(value: string | null | undefined, timezone = 'Asia/Shanghai') {
  return value ? formatUTCTime(value, timezone) : '尚无'
}

function SignaturePreview({ strokes }: { strokes: unknown }) {
  const paths = Array.isArray(strokes) ? strokes.filter(Array.isArray).map((stroke: unknown) => {
    const points = (stroke as unknown[]).filter((point): point is { x: number; y: number } => Boolean(point && typeof point === 'object' && typeof (point as any).x === 'number' && typeof (point as any).y === 'number'))
    if (!points.length) return null
    const d = `M ${Math.max(0, Math.min(1, points[0].x)) * 240} ${Math.max(0, Math.min(1, points[0].y)) * 90}` + points.slice(1).map(point => ` L ${Math.max(0, Math.min(1, point.x)) * 240} ${Math.max(0, Math.min(1, point.y)) * 90}`).join('')
    return <path key={d} d={d} fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
  }) : []
  return <svg viewBox="0 0 240 90" role="img" aria-label="手写签名" className="h-24 w-60 border border-dashed border-slate-300 bg-white">{paths}</svg>
}

function DrinkingReportDetail({ detail, timezone, onExport }: { detail: DrinkingReport & { notes: string; reporter_signature: unknown[]; leader_signature: unknown[] }; timezone: string; onExport: () => Promise<void> }) {
  const fields: Array<[string, string]> = [['姓名', detail.name], ['单位职务', detail.unit_position], ['饮酒时间', formatTime(detail.drinking_at, timezone)], ['饮酒地点', detail.drinking_place], ['饮酒事由', detail.reason], ['邀约人', detail.inviter], ['出行方式', detail.travel_method], ['责任领导姓名', detail.responsible_leader_name], ['备注说明', detail.notes], ['服务器收到时间', formatTime(detail.submitted_at, timezone)]]
  return <div className="grid gap-3"><div className="grid gap-2">{fields.map(([label, value]) => <div key={label}><b>{label}</b>：{value || '—'}</div>)}</div><div className="grid grid-cols-2 gap-3"><div><div className="mb-1">报备人签名</div><SignaturePreview strokes={detail.reporter_signature} /></div><div><div className="mb-1">责任领导签名</div><SignaturePreview strokes={detail.leader_signature} /></div></div><Button onClick={() => void onExport()}>导出 A4 PDF</Button></div>
}

export default function VenueCodeManagement() {
  const { user, systemTimezone } = useAuth()
  const canManage = Boolean(user?.permissions.includes('venue.manage'))
  const canExport = Boolean(user?.permissions.includes('venue.export'))
  const [venues, setVenues] = useState<VenueCodeItem[]>([])
  const [visits, setVisits] = useState<VenueVisitItem[]>([])
  const [cloud, setCloud] = useState<VenueCloudStatus | null>(null)
  const [editing, setEditing] = useState<VenueCodeItem | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [form] = Form.useForm<VenueCodeInput>()
  const [loading, setLoading] = useState(false)
  const [qrLoadingId, setQrLoadingId] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [visitFilters, setVisitFilters] = useState<{ venue_ids?: string; keyword?: string; start?: string; end?: string }>({})
  const [exportingVisits, setExportingVisits] = useState(false)
  const [exportProgress, setExportProgress] = useState<number | null>(0)
  const [drinkingForm, setDrinkingForm] = useState<DrinkingFormCode | null>(null)
  const [drinkingReports, setDrinkingReports] = useState<DrinkingReport[]>([])
  const [pullingCloud, setPullingCloud] = useState(false)
  const [drinkingFilters, setDrinkingFilters] = useState<{ keyword?: string; start?: string; end?: string }>({})

  const load = async (queries = { visits: visitFilters, drinking: drinkingFilters }) => {
    setLoading(true)
    setError('')
    try {
      const [venueResult, visitResult, cloudResult, drinkingResult, drinkingList] = await Promise.all([
        listVenueCodes(),
        listVenueVisits(queries.visits),
        getVenueCloudStatus(),
        getDrinkingFormCode(),
        listDrinkingReports(queries.drinking),
      ])
      setVenues(venueResult.data)
      setVisits(visitResult.data)
      setCloud(cloudResult)
      setDrinkingForm(drinkingResult)
      setDrinkingReports(drinkingList.data)
    } catch (reason: unknown) {
      setError(apiErrorMessage(reason, '二维码数据加载失败'))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [])

  const save = async () => {
    const values = await form.validateFields()
    try {
      if (editing) {
        await updateVenueCode(editing.id, values)
        message.success('场所配置已保存')
      } else {
        const result = await createVenueCode(values)
        if (result.cloud_sync_status === 'pending') {
          Modal.success({ title: '场所已创建', content: '配置正在同步到云端，确认完成后才可查看和打印二维码。' })
        } else {
          Modal.success({ title: '场所已创建', content: `扫码地址：${result.url || '请刷新后查看'}` })
        }
      }
      setModalOpen(false)
      await load()
    } catch (reason: unknown) {
      message.error(apiErrorMessage(reason, '保存失败'))
    }
  }

  const exportZip = async () => {
    if (exportingVisits) return
    setExportingVisits(true)
    setExportProgress(0)
    try {
      const blob = await exportVenueVisitsZip(visitFilters, setExportProgress)
      await downloadBlob(blob, `场所登记-${new Date().toISOString().slice(0, 10)}.zip`)
      setExportProgress(100)
      message.success('登记查询结果已导出')
    } catch (reason: unknown) {
      message.error(apiErrorMessage(reason, '压缩包导出失败'))
    } finally {
      setExportingVisits(false)
    }
  }
  const searchVisits = async (values: { venue_ids?: number[]; keyword?: string; range?: [any, any] }) => {
    const filters = { venue_ids: values.venue_ids?.length ? values.venue_ids.join(',') : undefined, keyword: values.keyword?.trim() || undefined, start: values.range?.[0]?.toISOString(), end: values.range?.[1]?.toISOString() }
    setVisitFilters(filters)
    try { const result = await listVenueVisits(filters); setVisits(result.data) } catch (reason: unknown) { message.error(apiErrorMessage(reason, '登记查询失败')) }
  }

  const showVenueQr = async (row: VenueCodeItem) => {
    setQrLoadingId(row.id)
    try {
      const q = await getVenueCodeQr(row.id)
      Modal.info({
        title: '场所二维码',
        content: (
          <div className="grid justify-items-center gap-3">
            <AuthenticatedImage alt="场所二维码" src={q.image_url} style={{ width: 220, height: 220 }} />
            <p className="m-0 break-all">扫码地址：{q.url}</p>
            {q.rotation_pending && <Alert type="warning" showIcon message="新二维码仍在同步，当前展示的是仍然有效的旧二维码。" />}
          </div>
        ),
      })
    } catch (reason: unknown) {
      message.error(apiErrorMessage(reason, '二维码读取失败，请稍后重试'))
    } finally {
      setQrLoadingId(current => current === row.id ? null : current)
    }
  }

  const showDrinkingQr = async () => {
    try {
      const q = await getDrinkingFormQr()
      Modal.info({ title: '饮酒报备二维码', content: <div className="grid justify-items-center gap-3"><AuthenticatedImage alt="饮酒报备二维码" src={q.image_url} style={{ width: 240, height: 240 }} /><p className="m-0 break-all">扫码地址：{q.url}</p></div> })
    } catch (reason: unknown) { message.error(apiErrorMessage(reason, '二维码读取失败')) }
  }

  const columns = [
    { title: '场所名称', dataIndex: 'name' },
    { title: '类型', dataIndex: 'venue_type' },
    { title: '地址', dataIndex: 'address' },
    { title: '所属社区', dataIndex: 'community_name' },
    {
      title: '本地状态',
      dataIndex: 'status',
      render: (value: VenueCodeItem['status'], row: VenueCodeItem) => {
        const waiting = row.cloud_sync_status === 'pending' || row.cloud_sync_status === 'error'
        const label = value === 'active'
          ? '启用'
          : value === 'inactive'
            ? waiting ? '请求停用' : '停用'
            : waiting ? '请求移除' : '已移除'
        return <Tag color={value === 'active' ? 'success' : value === 'deleted' ? 'error' : 'default'}>{label}</Tag>
      },
    },
    {
      title: '云端状态',
      render: (_: unknown, row: VenueCodeItem) => (
        <Space direction="vertical" size={2}>
          {cloudState(row)}
          {row.cloud_sync_error_code && <span className="text-xs text-red-600">{row.cloud_sync_error_code}</span>}
        </Space>
      ),
    },
    {
      title: '操作',
      render: (_: unknown, row: VenueCodeItem) => {
        const qrReady = row.status === 'active'
          && (row.cloud_sync_status === 'local_only' || row.cloud_synced_revision != null)
        return (
          <Space wrap>
            <Button
              type="link"
              icon={<QrcodeOutlined />}
              loading={qrLoadingId === row.id}
              disabled={!qrReady}
              onClick={() => void showVenueQr(row)}
            >
              二维码
            </Button>
            {canManage && row.status !== 'deleted' && <>
              <Button type="link" onClick={() => { setEditing(row); form.setFieldsValue(row); setModalOpen(true) }}>编辑</Button>
              <Popconfirm
                title="重新生成二维码？"
                description="云端确认新二维码前，旧二维码仍然有效。"
                onConfirm={async () => {
                  const result = await rotateVenueCodeToken(row.id)
                  message.success(result.message || '二维码已重新生成')
                  await load()
                }}
              >
                <Button type="link">轮换</Button>
              </Popconfirm>
              <Popconfirm
                title="移除这个场所？"
                description="既有登记记录仍按原期限保留；启用云端同步时，需等待云端确认后才算停用完成。"
                okText="移除"
                cancelText="取消"
                okButtonProps={{ danger: true }}
                onConfirm={async () => {
                  try {
                    await deleteVenueCode(row.id)
                    message.success('移除请求已保存')
                    await load()
                  } catch (reason: unknown) {
                    message.error(apiErrorMessage(reason, '移除失败'))
                  }
                }}
              >
                <Button type="link" danger icon={<DeleteOutlined />}>移除</Button>
              </Popconfirm>
            </>}
          </Space>
        )
      },
    },
  ]

  const cloudWarning = cloud && (cloud.outbox_failed > 0 || cloud.uncertain_count > 0 || cloud.last_error_code)

  return (
    <div className="app-page min-w-0">
      <PageHeader title="二维码管理" description="场所登记和饮酒报备均由云端接收、本地平台主动拉回。" />
      {error && <Alert type="error" showIcon message={error} />}
      {cloud && (
        <Alert
          type={cloudWarning ? 'warning' : cloud.enabled ? 'success' : 'info'}
          showIcon
          message={cloud.enabled ? '场所码云端链路' : '场所码云端链路未启用'}
          description={cloud.enabled
            ? `待同步 ${cloud.outbox_pending} 项，失败 ${cloud.outbox_failed} 项，待人工对账 ${cloud.uncertain_count} 项；最近拉取 ${cloud.last_pull_count} 条（入库 ${cloud.last_pull_accepted}、拒绝 ${cloud.last_pull_rejected}、稍后重试 ${cloud.last_pull_retry_later}、不确定 ${cloud.last_pull_uncertain}）；最近成功：${formatTime(cloud.last_success_at, systemTimezone)}`
            : '当前仍使用本地场所码入口。生产切换前应保持此状态，完成影子验收后再逐项启用开关。'}
        />
      )}
      <Tabs defaultActiveKey="venue" items={[{ key: 'venue', label: '场所登记码', children: <>
      <Panel title="场所目录" padded={false}>
        <div className="p-4 flex flex-wrap items-center justify-between gap-3">
          <span>共 {venues.length} 个场所</span>
          <Space wrap>
            {canManage && <Button type="primary" icon={<PlusOutlined />} onClick={() => { setEditing(null); form.setFieldsValue(emptyVenue); setModalOpen(true) }}>新增场所</Button>}
            <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
          </Space>
        </div>
        <Table rowKey="id" loading={loading} columns={columns} dataSource={venues} pagination={{ pageSize: 20 }} scroll={{ x: 1120 }} />
      </Panel>
      <Panel title="最近登记记录" padded={false}>
        <div className="grid gap-3 p-4">
          <div className="flex flex-wrap items-end gap-3">
            <Form layout="inline" onFinish={searchVisits} className="flex flex-1 flex-wrap items-end gap-3">
              <Form.Item name="venue_ids" label="场所" className="mb-0"><Select mode="multiple" allowClear maxTagCount="responsive" placeholder="全部场所" style={{ width: 280 }} options={venues.filter(item => item.status !== 'deleted').map(item => ({ label: item.name, value: item.id }))} /></Form.Item>
              <Form.Item name="keyword" className="mb-0"><Input allowClear placeholder="姓名、身份证号、手机号、地址" style={{ width: 260 }} /></Form.Item>
              <Form.Item name="range" className="mb-0"><DatePicker.RangePicker showTime /></Form.Item>
              <Button type="primary" htmlType="submit">查询</Button>
            </Form>
            {canExport && <Button icon={<DownloadOutlined />} loading={exportingVisits} disabled={exportingVisits} onClick={exportZip}>导出查询结果（ZIP）</Button>}
          </div>
          {exportingVisits && <div className="grid max-w-xl gap-1" role="status" aria-live="polite">
            <span className="text-xs text-[var(--app-text-secondary)]">正在生成并下载 ZIP；场所留空表示导出全部场所。请勿重复点击。</span>
            <Progress percent={exportProgress ?? 0} status="active" size="small" />
          </div>}
        </div>
        <Table
          rowKey="id"
          loading={loading}
          dataSource={visits}
          pagination={{ pageSize: 20 }}
          columns={[
            { title: '场所', dataIndex: 'venue_name' },
            { title: '姓名', dataIndex: 'name' },
            { title: '身份证号', dataIndex: 'identity_number' },
            { title: '手机号', dataIndex: 'phone' },
            { title: '地址', dataIndex: 'address' },
            { title: '登记时间', dataIndex: 'submitted_at' },
            {
              title: '照片',
              dataIndex: 'photo',
              render: (photo: VenueVisitItem['photo'], row: VenueVisitItem) => photo
                ? <Button
                    type="link"
                    icon={<EyeOutlined />}
                    onClick={() => Modal.info({
                      title: '登记照片',
                      width: 560,
                      content: (
                        <div className="flex min-h-40 items-center justify-center py-2">
                          <AuthenticatedImage
                            alt="登记照片"
                            src={getVenueVisitPhotoUrl(row.id)}
                            style={{ maxWidth: '100%', maxHeight: '60vh', objectFit: 'contain' }}
                          />
                        </div>
                      ),
                    })}
                  >查看照片</Button>
                  : <span className="text-[var(--app-text-secondary)]">无</span>,
            },
            ...(canManage ? [{
              title: '操作',
              render: (_: unknown, row: VenueVisitItem) => (
                <Popconfirm
                  title="删除这条登记记录？"
                  description="删除后记录和照片将从当前查询中隐藏，历史审计仍会保留。"
                  okText="删除"
                  cancelText="取消"
                  okButtonProps={{ danger: true }}
                  onConfirm={async () => {
                    try { await deleteVenueVisit(row.id); message.success('登记记录已删除'); await load() }
                    catch (reason: unknown) { message.error(apiErrorMessage(reason, '删除失败')) }
                  }}
                >删除</Popconfirm>
              ),
            }] : []),
          ]}
          scroll={{ x: 900 }}
        />
      </Panel>
      </> }, { key: 'drinking', label: '饮酒报备码', children: <>
        <Panel title="全所饮酒报备二维码" padded={false}>
          <div className="p-4 flex flex-wrap items-center gap-3">
            <Tag color={drinkingForm?.status === 'active' ? 'success' : 'default'}>{drinkingForm?.status === 'active' ? '已启用' : '未启用'}</Tag>
            <Tag>{drinkingForm?.cloud_sync_status === 'confirmed' ? '云端已确认' : drinkingForm?.cloud_sync_status === 'pending' ? '云端同步中' : '尚未创建'}</Tag>
            <Space wrap>
              {canManage && !drinkingForm?.exists && <Button type="primary" onClick={async () => { try { await createDrinkingFormCode(); message.success('饮酒报备二维码已创建'); await load() } catch (reason: unknown) { message.error(apiErrorMessage(reason, '创建失败')) } }}>创建二维码</Button>}
              {canManage && drinkingForm?.exists && <Button onClick={async () => { try { await updateDrinkingFormStatus(drinkingForm.status === 'active' ? 'inactive' : 'active'); await load() } catch (reason: unknown) { message.error(apiErrorMessage(reason, '更新失败')) } }}>{drinkingForm.status === 'active' ? '停用' : '启用'}</Button>}
              {canManage && drinkingForm?.exists && <Popconfirm title="轮换饮酒报备二维码？" onConfirm={async () => { try { await rotateDrinkingFormCode(); message.success('二维码轮换请求已提交'); await load() } catch (reason: unknown) { message.error(apiErrorMessage(reason, '轮换失败')) } }}><Button>轮换</Button></Popconfirm>}
              {drinkingForm?.exists && drinkingForm.status === 'active' && (drinkingForm.cloud_sync_status === 'confirmed' || drinkingForm.cloud_sync_status === 'local_only') && <Button icon={<QrcodeOutlined />} onClick={() => void showDrinkingQr()}>查看二维码</Button>}
            </Space>
          </div>
        </Panel>
        <Panel title="饮酒报备记录" padded={false}>
          <div className="p-4 flex flex-wrap items-center gap-3"><Form layout="inline" onFinish={async values => { const filters = { keyword: values.keyword?.trim() || undefined, start: values.range?.[0]?.toISOString(), end: values.range?.[1]?.toISOString() }; setDrinkingFilters(filters); try { const result = await listDrinkingReports(filters); setDrinkingReports(result.data) } catch (reason: unknown) { message.error(apiErrorMessage(reason, '饮酒报备查询失败')) } }}><Form.Item name="keyword"><Input allowClear placeholder="姓名、单位、地点、事由、责任领导" style={{ width: 280 }} /></Form.Item><Form.Item name="range"><DatePicker.RangePicker showTime /></Form.Item><Button type="primary" htmlType="submit">查询</Button></Form><Button icon={<ReloadOutlined />} loading={pullingCloud} onClick={async () => { setPullingCloud(true); try { const result = await pullVenueCloudNow(); if (result.accepted > 0) message.success(`已拉取 ${result.accepted} 条饮酒报备并入库`); else if (result.rejected > 0) message.warning(`云端有 ${result.rejected} 条登记被本地校验拒绝，请检查云端状态`); else if (result.retry_later > 0) message.warning(`云端有 ${result.retry_later} 条登记暂时未入库，将自动重试${result.reason_codes?.length ? `（${result.reason_codes.join('、')}）` : ''}`); else if (result.uncertain > 0) message.warning(`有 ${result.uncertain} 条登记处于不确定状态，请人工对账`); else message.info('当前没有可处理的云端登记'); await load() } catch (reason: unknown) { message.error(apiErrorMessage(reason, '云端拉取失败')) } finally { setPullingCloud(false) } }}>刷新</Button></div>
          <Table rowKey="id" dataSource={drinkingReports} scroll={{ x: 1100 }} columns={[{ title: '姓名', dataIndex: 'name' }, { title: '单位职务', dataIndex: 'unit_position' }, { title: '饮酒时间', dataIndex: 'drinking_at', render: (value: string | null) => formatTime(value, systemTimezone) }, { title: '饮酒地点', dataIndex: 'drinking_place' }, { title: '邀约人', dataIndex: 'inviter' }, { title: '责任领导', dataIndex: 'responsible_leader_name' }, { title: '操作', render: (_: unknown, row: DrinkingReport) => <Button type="link" onClick={async () => { try { const detail = await getDrinkingReport(row.id); Modal.info({ title: '饮酒报备详情', width: 760, content: <DrinkingReportDetail detail={detail} timezone={systemTimezone} onExport={async () => { const blob = await exportDrinkingReportPdf(row.id); await downloadBlob(blob, `饮酒报备单-${row.name}.pdf`) }} /> }) } catch (reason: unknown) { message.error(apiErrorMessage(reason, '详情读取失败')) } }}>查看详情</Button> }]} />
        </Panel>
      </> }]}/>
      <Modal title={editing ? '编辑场所' : '新增场所'} open={modalOpen} onCancel={() => setModalOpen(false)} onOk={save}>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="场所名称" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="venue_type" label="场所类型"><Input /></Form.Item>
          <Form.Item name="address" label="地址"><Input /></Form.Item>
          <Form.Item name="community_name" label="所属社区"><Input /></Form.Item>
          <Form.Item name="status" label="状态">
            <Select options={[{ label: '启用', value: 'active' }, { label: '停用', value: 'inactive' }]} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

export function PublicVenuePage() {
  const token = window.location.pathname.split('/').pop() || ''
  const [info, setInfo] = useState<{ venue_id: number; name: string; form_token: string } | null>(null)
  const [form] = Form.useForm()
  const [done, setDone] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    import('../api/client').then(({ getPublicVenueInfo }) => getPublicVenueInfo(token).then(setInfo).catch((reason: unknown) => setError(apiErrorMessage(reason, '二维码无效'))))
  }, [token])
  const submit = async (values: Record<string, any>) => {
    if (!info || !values.photo?.file) {
      throw new Error('请选择照片')
    }
    const body = new FormData()
    Object.entries({ ...values, venue_id: info?.venue_id, form_token: info?.form_token }).forEach(([key, value]) => {
      if (key !== 'photo' && value != null) body.append(key, String(value))
    })
    body.append('photo', values.photo.file)
    const response = await fetch(resolveRuntimeApiUrl('/api/public/venue-visits'), { method: 'POST', body })
    if (!response.ok) {
      const payload = await readVenueErrorPayload(response)
      throw new Error(venueRegistrationErrorMessage(response.status, payload))
    }
    setDone(true)
  }
  if (error) return <div className="p-6"><Alert type="error" message={error} /></div>
  if (done) return <div className="p-6"><Alert type="success" message="登记成功" /></div>
  return (
    <div className="max-w-xl mx-auto p-6">
      <h2>{info?.name || '场所登记'}</h2>
      <Form form={form} layout="vertical" onFinish={values => submit(values).catch(reason => setError(reason.message))}>
        <Form.Item name="name" label="姓名" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="identity_number" label="公民身份号码" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="phone" label="手机号" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="address" label="地址" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="photo" label="照片" valuePropName="file" getValueFromEvent={event => event} rules={[{ required: true, message: '请选择照片' }]}>
          <Upload beforeUpload={() => false} maxCount={1} accept="image/jpeg,image/png,image/webp"><Button>选择照片</Button></Upload>
        </Form.Item>
        <Button type="primary" htmlType="submit" disabled={!info}>提交登记</Button>
      </Form>
    </div>
  )
}
