import { useEffect, useState } from 'react'
import { Alert, Button, Form, Input, Modal, Popconfirm, Space, Table, Tag, Upload, message } from 'antd'
import { PlusOutlined, QrcodeOutlined, ReloadOutlined, DownloadOutlined } from '@ant-design/icons'
import {
  createVenueCode, exportVenueVisits, getVenueCodeQr, listVenueCodes, listVenueVisits,
  rotateVenueCodeToken, updateVenueCode, type VenueCodeItem, type VenueVisitItem,
} from '../api/client'
import { downloadBlob } from '../utils/fileDownload'
import { PageHeader, Panel } from '../components/ui'
import { useAuth } from '../context/AuthContext'

const emptyVenue = { name: '', venue_type: '', address: '', community_id: null, community_name: '', status: 'active' as const }

export default function VenueCodeManagement() {
  const { user } = useAuth()
  const canManage = Boolean(user?.permissions.includes('venue.manage'))
  const canExport = Boolean(user?.permissions.includes('venue.export'))
  const [venues, setVenues] = useState<VenueCodeItem[]>([])
  const [visits, setVisits] = useState<VenueVisitItem[]>([])
  const [editing, setEditing] = useState<VenueCodeItem | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [form] = Form.useForm()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const load = async () => {
    setLoading(true); setError('')
    try { const [v, r] = await Promise.all([listVenueCodes(), listVenueVisits()]); setVenues(v.data); setVisits(r.data) }
    catch (e: any) { setError(e?.response?.data?.detail || '场所码数据加载失败') }
    finally { setLoading(false) }
  }
  useEffect(() => { void load() }, [])
  const save = async () => {
    const values = await form.validateFields()
    try {
      if (editing) await updateVenueCode(editing.id, values)
      else { const result = await createVenueCode(values); Modal.success({ title: '场所已创建', content: `扫码地址：${result.url}` }) }
      setModalOpen(false); await load()
    } catch (e: any) { message.error(e?.response?.data?.detail || '保存失败') }
  }
  const exportRows = async () => { try { const blob = await exportVenueVisits(); await downloadBlob(blob, `场所登记-${new Date().toISOString().slice(0, 10)}.xlsx`) } catch (e: any) { message.error(e?.response?.data?.detail || '导出失败') } }
  const columns = [
    { title: '场所名称', dataIndex: 'name' }, { title: '类型', dataIndex: 'venue_type' }, { title: '地址', dataIndex: 'address' }, { title: '所属社区', dataIndex: 'community_name' },
    { title: '状态', dataIndex: 'status', render: (v: string) => <Tag color={v === 'active' ? 'success' : 'default'}>{v === 'active' ? '启用' : '停用'}</Tag> },
    { title: '操作', render: (_: unknown, row: VenueCodeItem) => <Space><Button type="link" icon={<QrcodeOutlined />} onClick={async () => { const q = await getVenueCodeQr(row.id); Modal.info({ title: '场所二维码', content: <><img alt="场所二维码" src={q.image_url} style={{ width: 220, height: 220 }} /><p>扫码地址：{q.url}</p></> }) }}>二维码</Button>{canManage && <><Button type="link" onClick={() => { setEditing(row); form.setFieldsValue(row); setModalOpen(true) }}>编辑</Button><Popconfirm title="重新生成二维码？" onConfirm={async () => { await rotateVenueCodeToken(row.id); message.success('二维码已重新生成'); await load() }}><Button type="link">轮换</Button></Popconfirm></>}</Space> },
  ]
  return <div className="app-page min-w-0"><PageHeader title="场所码管理" description="场所码与流口指令核查相互独立，仅用于匿名扫码登记。" />{error && <Alert type="error" showIcon message={error} />}<Panel title="场所目录" padded={false}><div className="p-4 flex justify-between"><span>共 {venues.length} 个场所</span><Space>{canExport && <Button icon={<DownloadOutlined />} onClick={exportRows}>导出登记记录</Button>}{canManage && <Button type="primary" icon={<PlusOutlined />} onClick={() => { setEditing(null); form.setFieldsValue(emptyVenue); setModalOpen(true) }}>新增场所</Button>}<Button icon={<ReloadOutlined />} onClick={load}>刷新</Button></Space></div><Table rowKey="id" loading={loading} columns={columns} dataSource={venues} pagination={{ pageSize: 20 }} scroll={{ x: 900 }} /></Panel><Panel title="最近登记记录" padded={false}><Table rowKey="id" loading={loading} dataSource={visits} pagination={{ pageSize: 20 }} columns={[{ title: '场所', dataIndex: 'venue_name' }, { title: '姓名', dataIndex: 'name' }, { title: '身份证号', dataIndex: 'identity_number' }, { title: '手机号', dataIndex: 'phone' }, { title: '地址', dataIndex: 'address' }, { title: '登记时间', dataIndex: 'submitted_at' }]} scroll={{ x: 900 }} /></Panel><Modal title={editing ? '编辑场所' : '新增场所'} open={modalOpen} onCancel={() => setModalOpen(false)} onOk={save}><Form form={form} layout="vertical"><Form.Item name="name" label="场所名称" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="venue_type" label="场所类型"><Input /></Form.Item><Form.Item name="address" label="地址"><Input /></Form.Item><Form.Item name="community_name" label="所属社区"><Input /></Form.Item><Form.Item name="status" label="状态"><Input /></Form.Item></Form></Modal></div>
}

export function PublicVenuePage() {
  const token = window.location.pathname.split('/').pop() || ''
  const [info, setInfo] = useState<{ venue_id: number; name: string; form_token: string } | null>(null)
  const [form] = Form.useForm(); const [done, setDone] = useState(false); const [error, setError] = useState('')
  useEffect(() => { import('../api/client').then(({ getPublicVenueInfo }) => getPublicVenueInfo(token).then(setInfo).catch((e: any) => setError(e?.response?.data?.detail || '二维码无效'))) }, [token])
  const submit = async (values: any) => { const body = new FormData(); Object.entries({ ...values, venue_id: info?.venue_id, form_token: info?.form_token }).forEach(([k, v]) => { if (k !== 'photo' && v != null) body.append(k, String(v)) }); body.append('photo', values.photo.file); const response = await fetch('/api/public/venue-visits', { method: 'POST', body }); if (!response.ok) { const payload = await response.json().catch(() => ({})); throw new Error(payload.detail || '提交失败') } setDone(true) }
  if (error) return <div className="p-6"><Alert type="error" message={error} /></div>
  if (done) return <div className="p-6"><Alert type="success" message="登记成功" /></div>
  return <div className="max-w-xl mx-auto p-6"><h2>{info?.name || '场所登记'}</h2><Form form={form} layout="vertical" onFinish={values => submit(values).catch(e => setError(e.message))}><Form.Item name="name" label="姓名" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="identity_number" label="公民身份号码" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="phone" label="手机号" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="address" label="地址" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="photo" label="照片" valuePropName="file" getValueFromEvent={e => e}><Upload beforeUpload={() => false} maxCount={1} accept="image/jpeg,image/png,image/webp"><Button>选择照片</Button></Upload></Form.Item><Button type="primary" htmlType="submit" disabled={!info}>提交登记</Button></Form></div>
}
