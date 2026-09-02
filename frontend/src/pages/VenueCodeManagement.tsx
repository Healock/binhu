import { useEffect, useState } from 'react'
import { Alert, Button, Form, Input, Modal, Popconfirm, Select, Space, Table, Tag, Upload, message } from 'antd'
import { DeleteOutlined, DownloadOutlined, PlusOutlined, QrcodeOutlined, ReloadOutlined } from '@ant-design/icons'
import {
  apiErrorMessage,
  createVenueCode,
  deleteVenueCode,
  exportVenueVisits,
  getVenueCloudStatus,
  getVenueCodeQr,
  listVenueCodes,
  listVenueVisits,
  rotateVenueCodeToken,
  updateVenueCode,
  type VenueCloudStatus,
  type VenueCodeInput,
  type VenueCodeItem,
  type VenueVisitItem,
} from '../api/client'
import { downloadBlob } from '../utils/fileDownload'
import { PageHeader, Panel } from '../components/ui'
import { useAuth } from '../context/AuthContext'
import AuthenticatedImage from '../components/AuthenticatedImage'
import { resolveRuntimeApiUrl } from '../utils/apiEnvironment'

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

function formatTime(value: string | null | undefined) {
  return value ? new Date(value).toLocaleString() : '尚无'
}

export default function VenueCodeManagement() {
  const { user } = useAuth()
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

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const [venueResult, visitResult, cloudResult] = await Promise.all([
        listVenueCodes(),
        listVenueVisits(),
        getVenueCloudStatus(),
      ])
      setVenues(venueResult.data)
      setVisits(visitResult.data)
      setCloud(cloudResult)
    } catch (reason: unknown) {
      setError(apiErrorMessage(reason, '场所码数据加载失败'))
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

  const exportRows = async () => {
    try {
      const blob = await exportVenueVisits()
      await downloadBlob(blob, `场所登记-${new Date().toISOString().slice(0, 10)}.xlsx`)
    } catch (reason: unknown) {
      message.error(apiErrorMessage(reason, '导出失败'))
    }
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
      <PageHeader title="场所码管理" description="场所配置由本地服务器管理，公开登记由云端接收后主动拉回。" />
      {error && <Alert type="error" showIcon message={error} />}
      {cloud && (
        <Alert
          type={cloudWarning ? 'warning' : cloud.enabled ? 'success' : 'info'}
          showIcon
          message={cloud.enabled ? '场所码云端链路' : '场所码云端链路未启用'}
          description={cloud.enabled
            ? `待同步 ${cloud.outbox_pending} 项，失败 ${cloud.outbox_failed} 项，待人工对账 ${cloud.uncertain_count} 项；最近成功：${formatTime(cloud.last_success_at)}`
            : '当前仍使用本地场所码入口。生产切换前应保持此状态，完成影子验收后再逐项启用开关。'}
        />
      )}
      <Panel title="场所目录" padded={false}>
        <div className="p-4 flex flex-wrap items-center justify-between gap-3">
          <span>共 {venues.length} 个场所</span>
          <Space wrap>
            {canExport && <Button icon={<DownloadOutlined />} onClick={exportRows}>导出登记记录</Button>}
            {canManage && <Button type="primary" icon={<PlusOutlined />} onClick={() => { setEditing(null); form.setFieldsValue(emptyVenue); setModalOpen(true) }}>新增场所</Button>}
            <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
          </Space>
        </div>
        <Table rowKey="id" loading={loading} columns={columns} dataSource={venues} pagination={{ pageSize: 20 }} scroll={{ x: 1120 }} />
      </Panel>
      <Panel title="最近登记记录" padded={false}>
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
          ]}
          scroll={{ x: 900 }}
        />
      </Panel>
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
    const body = new FormData()
    Object.entries({ ...values, venue_id: info?.venue_id, form_token: info?.form_token }).forEach(([key, value]) => {
      if (key !== 'photo' && value != null) body.append(key, String(value))
    })
    body.append('photo', values.photo.file)
    const response = await fetch(resolveRuntimeApiUrl('/api/public/venue-visits'), { method: 'POST', body })
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}))
      throw new Error(payload.detail || '提交失败')
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
        <Form.Item name="photo" label="照片" valuePropName="file" getValueFromEvent={event => event}>
          <Upload beforeUpload={() => false} maxCount={1} accept="image/jpeg,image/png,image/webp"><Button>选择照片</Button></Upload>
        </Form.Item>
        <Button type="primary" htmlType="submit" disabled={!info}>提交登记</Button>
      </Form>
    </div>
  )
}
