import { useEffect, useMemo, useState } from 'react'
import { Alert, Button, Card, Input, Progress, Segmented, Select, Space, Statistic, Tag, Upload, message } from 'antd'
import type { TableColumnsType, UploadFile, UploadProps } from 'antd'
import { DownloadOutlined, ExportOutlined, InboxOutlined, RightOutlined, UploadOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import AppTable from './AppTable'
import { Panel } from './ui'
import {
  confirmPoliceDispatchImport, getPoliceImportProfiles, listPoliceDispatchBatches,
  policeDispatchFeedbackUrl, policeDispatchSourceFileUrl, previewPoliceDispatchImport,
  type PoliceDispatchBatch, type PoliceImportPreview, type PoliceImportProfile,
} from '../api/client'

const { Dragger } = Upload
const businessOptions = [
  { value: 'fullchain', label: '全链条' }, { value: 'rental', label: '出租房屋核查' },
  { value: 'police', label: '涉警' }, { value: 'delivery', label: '寄递业' },
  { value: 'suspect_return', label: '疑似返苏' },
]
const businessLabels: Record<string, string> = Object.fromEntries(businessOptions.map(item => [item.value, item.label]))
const policeSubtypeLabels: Record<string, string> = {
  internal: '所内涉警',
  suzhou: '苏州涉警',
  traffic: '交通涉警',
}
const statusMeta: Record<string, { color: string; text: string }> = {
  reviewing: { color: 'processing', text: '审核中' }, ready_to_publish: { color: 'warning', text: '待发布' },
  publishing: { color: 'blue', text: '发布中' }, reconciling: { color: 'error', text: '待对账/冲突' },
  completed: { color: 'success', text: '已完成' },
}

function localDate(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
}

export default function PoliceDispatchPanel({ enabled }: { enabled: boolean }) {
  const navigate = useNavigate()
  const [profiles, setProfiles] = useState<PoliceImportProfile[]>([])
  const [businessType, setBusinessType] = useState('fullchain')
  const [profileKey, setProfileKey] = useState('fullchain_raw')
  const [businessDate, setBusinessDate] = useState(localDate())
  const [file, setFile] = useState<File | null>(null)
  const [fileList, setFileList] = useState<UploadFile[]>([])
  const [preview, setPreview] = useState<PoliceImportPreview | null>(null)
  const [previewToken, setPreviewToken] = useState('')
  const [batches, setBatches] = useState<PoliceDispatchBatch[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [fileName, setFileName] = useState('')
  const [uploadDate, setUploadDate] = useState('')
  const [status, setStatus] = useState('all')
  const [loading, setLoading] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const visibleProfiles = useMemo(() => profiles.filter(item => item.business_type === businessType), [profiles, businessType])
  const selectedProfile = profiles.find(item => item.key === profileKey)

  const load = async (targetPage = page) => {
    if (!enabled) return
    setLoading(true)
    try {
      const result = await listPoliceDispatchBatches({
        file_name: fileName || undefined, upload_date: uploadDate || undefined, status,
        business_type: businessType,
        police_subtype: businessType === 'police' && selectedProfile?.police_subtype ? selectedProfile.police_subtype : undefined,
        page: targetPage, page_size: 20,
      })
      setBatches(result.data); setTotal(result.total); setPage(targetPage); setError('')
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '下发批次读取失败')
    } finally { setLoading(false) }
  }

  useEffect(() => {
    if (!enabled) return
    getPoliceImportProfiles().then(result => setProfiles(result.data))
      .catch((reason: any) => setError(reason?.response?.data?.detail || '导入入口读取失败'))
  }, [enabled])
  useEffect(() => { void load(1) }, [enabled, status, businessType, profileKey])
  useEffect(() => {
    const candidates = profiles.filter(item => item.business_type === businessType)
    if (candidates.length && !candidates.some(item => item.key === profileKey)) setProfileKey(candidates[0].key)
    setFile(null); setFileList([]); setPreview(null); setPreviewToken('')
  }, [businessType, profiles])

  const resetPreview = () => { setPreview(null); setPreviewToken('') }
  const beforeUpload: UploadProps['beforeUpload'] = selected => {
    const lower = selected.name.toLowerCase()
    if (!lower.endsWith('.xls') && !lower.endsWith('.xlsx')) { message.error('只支持 .xls 或 .xlsx 文件'); return Upload.LIST_IGNORE }
    setFile(selected); resetPreview()
    setFileList([{ uid: selected.uid, name: selected.name, size: selected.size, status: 'done', originFileObj: selected }])
    return false
  }
  const handlePreview = async () => {
    if (!file || !selectedProfile) return
    setUploading(true)
    try {
      const result = await previewPoliceDispatchImport(file, selectedProfile.key, businessDate)
      setPreview(result.preview); setPreviewToken(result.preview_token); setError('')
      message.success('预览已生成，请核对统计和问题数据')
    } catch (reason: any) { setError(reason?.response?.data?.detail || '数据预览失败') }
    finally { setUploading(false) }
  }
  const handleConfirm = async () => {
    if (!file || !selectedProfile || !previewToken) return
    setUploading(true)
    try {
      const result = await confirmPoliceDispatchImport(file, selectedProfile.key, businessDate, previewToken)
      message.success(result.message); setFile(null); setFileList([]); resetPreview(); await load(1)
      const batchId = result.batch.id
      const counts = result.batch.counts
      if (counts.pending_review > 0) {
        navigate(`/police-tasks?batch=${batchId}&status=pending_review&category=manual`)
      } else if (counts.pending_publish > 0) {
        navigate(`/police-tasks?batch=${batchId}&status=pending_publish&category=all`)
      } else {
        navigate(`/police-dispatch/batches/${batchId}`)
      }
    } catch (reason: any) { setError(reason?.response?.data?.detail || '确认导入失败') }
    finally { setUploading(false) }
  }

  const columns: TableColumnsType<PoliceDispatchBatch> = [
    { title: '批次', dataIndex: 'id', width: 78, render: value => `#${value}` },
    { title: '业务', width: 150, render: (_, row) => row.police_subtype ? `涉警 · ${policeSubtypeLabels[row.police_subtype] || row.police_subtype}` : businessLabels[row.business_type] || row.target_parser },
    { title: '原文件', dataIndex: 'file_name', ellipsis: true, width: 230 },
    { title: '审核进度', width: 170, render: (_, item) => <Progress size="small" percent={item.total_count ? Math.round(item.reviewed_count / item.total_count * 100) : 0} format={() => `${item.reviewed_count}/${item.total_count}`} /> },
    { title: '状态', dataIndex: 'status', width: 110, render: value => <Tag color={statusMeta[value]?.color}>{statusMeta[value]?.text || value}</Tag> },
    { title: '操作', width: 300, fixed: 'right', render: (_, item) => <Space size={2}>
      <Button type="link" onClick={() => navigate(`/police-tasks?batch=${item.id}`)}>处理 <RightOutlined /></Button>
      {item.source_file_available && <Button type="link" icon={<DownloadOutlined />} href={policeDispatchSourceFileUrl(item.id)}>原文件</Button>}
      <Button type="link" icon={<ExportOutlined />} href={policeDispatchFeedbackUrl(item.id)}>反馈</Button>
    </Space> },
  ]
  const previewColumns: TableColumnsType<PoliceImportPreview['rows'][number]> = [
    { title: 'Excel 行', dataIndex: 'source_row', width: 90 }, { title: '姓名', dataIndex: 'person_name', width: 110 },
    { title: '身份证号', dataIndex: 'identity_number', width: 190 }, { title: '手机号', dataIndex: 'phone', width: 150 },
    { title: '社区', dataIndex: 'community_name', width: 130 },
    { title: '状态', dataIndex: 'result', width: 110, render: value => <Tag color={value === 'importable' ? 'green' : 'orange'}>{value === 'importable' ? '可导入' : '问题数据'}</Tag> },
    { title: '问题', width: 300, render: (_, row) => row.issues.length ? row.issues.map(item => `${item.field}：${item.value}`).join('；') : '—' },
  ]
  const latest = batches[0]

  return <Panel title="业务数据导入" description="上传中心只负责预览和确认导入；导入后请到下发工作台审核并选择发布" padded={false}>
    <div className="police-dispatch-panel__content">
      {!enabled && <Alert type="info" showIcon message="当前账号没有数据下发权限" />}
      {error && <Alert type="error" showIcon message={error} closable onClose={() => setError('')} />}
      <Segmented block value={businessType} onChange={value => setBusinessType(String(value))} options={businessOptions} />
      {visibleProfiles.length > 1 && <Segmented value={profileKey} onChange={value => { setProfileKey(String(value)); resetPreview() }} options={visibleProfiles.map(item => ({ value: item.key, label: item.label }))} />}
      {selectedProfile && <Card size="small"><div className="app-semantic-stack">
        <div className="flex flex-wrap items-center gap-2"><strong>{selectedProfile.label}</strong><Tag color={selectedProfile.enabled ? 'green' : 'default'}>{selectedProfile.enabled ? '可用' : '暂未开放'}</Tag><Tag color={selectedProfile.target_configured ? 'blue' : 'orange'}>{selectedProfile.target_configured ? '腾讯目标表已配置' : '腾讯目标表未配置'}</Tag></div>
        <div className="text-sm text-[var(--app-text-secondary)]">{selectedProfile.description}</div>
        <div className="flex flex-wrap gap-2">{selectedProfile.example_fields.map(field => <Tag key={field}>{field}</Tag>)}</div>
        {!selectedProfile.target_configured && ['police_suzhou_processed', 'police_traffic_processed'].includes(selectedProfile.key) && <Alert type="warning" showIcon message={`可以预览、导入和审核；配置唯一启用的“${selectedProfile.target_parser}”腾讯表前，发布会被后端阻止。`} />}
      </div></Card>}
      <div className="grid gap-3 md:grid-cols-[220px_minmax(0,1fr)] md:items-end">
        <label className="grid gap-1 text-sm"><span className="font-medium">业务日期</span><Input type="date" value={businessDate} onChange={event => { setBusinessDate(event.target.value); resetPreview() }} /></label>
        <Dragger accept=".xls,.xlsx" maxCount={1} fileList={fileList} beforeUpload={beforeUpload} onRemove={() => { setFile(null); setFileList([]); resetPreview() }} disabled={!enabled || uploading || !selectedProfile?.enabled}>
          <p className="ant-upload-drag-icon"><InboxOutlined /></p><p className="ant-upload-text">拖入 {selectedProfile?.label || '业务'} 文件，或点击选择</p><p className="ant-upload-hint">支持 .xls/.xlsx；短日期将结合上方业务日期补全年份</p>
        </Dragger>
      </div>
      <div className="flex flex-wrap justify-end gap-2"><Button type="primary" icon={<UploadOutlined />} disabled={!enabled || !file || !selectedProfile?.enabled} loading={uploading} onClick={() => void handlePreview()}>预览数据</Button><Button type="primary" disabled={!previewToken} loading={uploading} onClick={() => void handleConfirm()}>确认导入到下发工作台</Button></div>
      {preview && <div className="police-dispatch-panel__preview"><div className="app-semantic-stack">
        <Alert type={preview.counts.conflict ? 'warning' : 'success'} showIcon message={`共 ${preview.row_count} 条，可导入 ${preview.counts.importable} 条，问题数据 ${preview.row_count - preview.counts.importable} 条`} description="确认导入不会自动发布；问题行会保留在工作台等待人工处理。" />
        <div className="grid gap-3 sm:grid-cols-3 xl:grid-cols-7">{[
          ['总数', preview.counts.total], ['可导入', preview.counts.importable], ['缺少主键', preview.counts.missing_key],
          ['重复', preview.counts.duplicate], ['身份证异常', preview.counts.identity_invalid], ['社区异常', preview.counts.community_invalid], ['冲突', preview.counts.conflict],
        ].map(([label, value]) => <div key={String(label)} className="police-dispatch-summary-card rounded-xl p-3"><Statistic title={label} value={value} suffix="条" valueStyle={{ fontSize: 20 }} /></div>)}</div>
        <div className="flex flex-wrap gap-2">{preview.community_distribution.map(item => <Tag key={item.community_id} color="blue">{item.community_name} {item.count} 条</Tag>)}</div>
        <AppTable rowKey="source_row" columns={previewColumns} dataSource={preview.rows} pagination={false} scroll={{ x: 1080, y: 420 }} size="small" />
        {preview.rows_truncated && <div className="text-xs text-[var(--app-text-secondary)]">明细仅展示前 100 行，统计覆盖完整文件。</div>}
      </div></div>}
      {latest && <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">{[
        ['最近批次总数', latest.counts.total], ['待审核', latest.counts.pending_review], ['待发布', latest.counts.pending_publish], ['已发布', latest.counts.published],
      ].map(([label, value]) => <div key={String(label)} className="police-dispatch-summary-card rounded-xl p-3"><Statistic title={label} value={value} suffix="条" valueStyle={{ fontSize: 22 }} /></div>)}</div>}
      <div className="grid gap-2 md:grid-cols-[minmax(0,1fr)_180px_170px_auto]"><Input.Search allowClear value={fileName} placeholder="按原文件名筛选" onChange={event => setFileName(event.target.value)} onSearch={() => void load(1)} /><Input type="date" value={uploadDate} onChange={event => setUploadDate(event.target.value)} /><Select value={status} onChange={setStatus} options={[{ value: 'all', label: '全部状态' }, { value: 'reviewing', label: '审核中' }, { value: 'ready_to_publish', label: '待发布' }, { value: 'reconciling', label: '待对账/冲突' }, { value: 'completed', label: '已完成' }]} /><Button onClick={() => void load(1)}>筛选</Button></div>
      <AppTable<PoliceDispatchBatch> rowKey="id" columns={columns} dataSource={batches} loading={loading} pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: nextPage => void load(nextPage) }} scroll={{ x: 1100 }} size="small" />
    </div>
  </Panel>
}
