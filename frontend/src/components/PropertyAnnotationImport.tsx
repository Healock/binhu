import { UploadOutlined } from '@ant-design/icons'
import { Alert, Button, Modal, Select, Tag, Upload, message } from 'antd'
import { useState } from 'react'
import { registryApi, type PropertyAnnotationPreview, type RegistrySmallCommunityOption } from '../api/client'
import { useResponsiveLayout } from '../hooks/useResponsiveLayout'
import AppTable from './AppTable'
import type { ResponsiveColumns } from './responsiveTable'

type PreviewRow = PropertyAnnotationPreview['items'][number]
type PreviewStatus = keyof typeof states
type PreviewStatusFilter = 'all' | PreviewStatus
const states = {
  ready: { label: '可确认', color: 'blue' },
  blocked: { label: '拒绝应用', color: 'error' },
  review: { label: '待人工复核', color: 'gold' },
  skipped: { label: '跳过', color: 'default' },
  applied: { label: '已应用', color: 'success' },
}

export default function PropertyAnnotationImport({ onChanged }: { onChanged: () => Promise<void> }) {
  const layout = useResponsiveLayout()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState<PropertyAnnotationPreview | null>(null)
  const [selected, setSelected] = useState<number[]>([])
  const [error, setError] = useState('')
  const [applyFailed, setApplyFailed] = useState(false)
  const [statusFilter, setStatusFilter] = useState<PreviewStatusFilter>('all')
  const [manualRow, setManualRow] = useState<PreviewRow | null>(null)
  const [manualOptions, setManualOptions] = useState<RegistrySmallCommunityOption[]>([])
  const [manualSmallCommunityId, setManualSmallCommunityId] = useState<number>()
  const [manualLoading, setManualLoading] = useState(false)
  const [manualSaving, setManualSaving] = useState(false)

  const read = async (file: File) => {
    setBusy(true)
    setPreview(null)
    setSelected([])
    setError('')
    setApplyFailed(false)
    setStatusFilter('all')
    try {
      const result = await registryApi.previewPropertyAnnotations(file)
      setPreview(result)
      setSelected(result.items.filter(row => row.status === 'ready' && !row.replaces_manual).map(row => row.xlsx_row))
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '标注工作簿预览失败，请检查文件后重新选择')
    } finally {
      setBusy(false)
    }
  }

  const apply = async () => {
    if (!preview || busy || applyFailed) return
    const rows = preview.items.filter(row => selected.includes(row.xlsx_row) && row.status === 'ready' && row.apply_item)
    if (!rows.length) return
    setBusy(true)
    setError('')
    let applied = 0
    try {
      for (let offset = 0; offset < rows.length; offset += 200) {
        const batch = rows.slice(offset, offset + 200)
        const result = await registryApi.applyPropertyAnnotations(batch.map(row => row.apply_item!))
        applied += result.confirmed
        const keys = new Set(batch.map(row => row.xlsx_row))
        setPreview(current => current && ({ ...current, items: current.items.map(row => keys.has(row.xlsx_row) ? { ...row, status: 'applied', reason: '已人工确认' } : row) }))
        setSelected(current => current.filter(key => !keys.has(key)))
      }
      message.success(`已确认 ${applied} 套房屋的小区归属`)
    } catch (reason: any) {
      setApplyFailed(true)
      setError(`本次已确认 ${applied} 套。${reason?.response?.data?.detail || '请求结果不确定，请刷新房屋列表核对'}。请重新上传预览后再处理其余记录。`)
    } finally {
      setBusy(false)
      await onChanged()
    }
  }

  const openManualConfirm = async (row: PreviewRow) => {
    if (!row.property_id || !row.community_id || busy || applyFailed) return
    setManualRow(row)
    setManualOptions([])
    setManualSmallCommunityId(undefined)
    setManualLoading(true)
    try {
      const result = await registryApi.smallCommunityOptions(row.community_id)
      setManualOptions(result.data)
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '读取当前社区小区失败，请稍后重试')
      setManualRow(null)
    } finally {
      setManualLoading(false)
    }
  }

  const confirmManual = async () => {
    if (!manualRow?.property_id || !manualSmallCommunityId || manualSaving) return
    const target = manualOptions.find(option => option.id === manualSmallCommunityId)
    if (!target) return
    setManualSaving(true)
    try {
      const snapshot = manualRow.manual_confirm_item
      if (!snapshot) {
        message.error('预览缺少房屋版本信息，请重新上传工作簿')
        return
      }
      await registryApi.confirmPropertySmallCommunities([{
        property_id: snapshot.property_id,
        small_community_id: target.id,
        expected_snapshot: snapshot.expected_snapshot,
        expected_match_snapshot: snapshot.expected_match_snapshot,
      }])
      setPreview(current => current && ({ ...current, items: current.items.map(row => row.xlsx_row === manualRow.xlsx_row
        ? { ...row, status: 'applied', target_name: target.name, reason: '已人工确认', annotation_reason: row.annotation_reason || '管理员核实后确认' }
        : row) }))
      setManualRow(null)
      message.success('已确认该房屋的小区归属')
      try {
        await onChanged()
      } catch {
        message.warning('小区已确认，但房屋列表刷新失败，请手动刷新')
      }
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '人工确认失败，请刷新后重试')
    } finally {
      setManualSaving(false)
    }
  }

  const selectedRows = preview?.items.filter(row => selected.includes(row.xlsx_row) && row.status === 'ready') || []
  const replacementCount = selectedRows.filter(row => row.replaces_manual).length
  const counts = Object.fromEntries(Object.keys(states).map(state => [state, preview?.items.filter(row => row.status === state).length || 0])) as Record<PreviewStatus, number>
  const visibleItems = preview?.items.filter(row => statusFilter === 'all' || row.status === statusFilter) || []
  const statusOptions: Array<{ value: PreviewStatusFilter; label: string }> = [
    { value: 'all', label: `全部状态（${preview?.items.length || 0}）` },
    ...Object.entries(states).map(([value, state]) => ({ value: value as PreviewStatus, label: `${state.label}（${counts[value as PreviewStatus]}）` })),
  ]
  const columns: ResponsiveColumns<PreviewRow> = [
    { title: '文件行', dataIndex: 'xlsx_row', width: 80, responsivePriority: 'always' },
    { title: '房屋地址', dataIndex: 'address', width: 240, responsivePriority: 'always', render: value => <span style={{ overflowWrap: 'anywhere' }}>{value || '无可查看地址'}</span> },
    { title: '社区', dataIndex: 'community', width: 120, responsivePriority: 'standard' },
    { title: '标注小区', dataIndex: 'target_name', width: 170, responsivePriority: 'always', render: (value, row) => <div className="grid gap-2"><span>{value || '未选择'}</span>{row.replaces_manual && <Tag color="warning">替换已有人工确认</Tag>}</div> },
    { title: '状态', dataIndex: 'status', width: 120, responsivePriority: 'always', render: value => <Tag color={states[value as keyof typeof states].color}>{states[value as keyof typeof states].label}</Tag> },
    { title: '依据与处理原因', key: 'reason', width: 270, responsivePriority: 'always', render: (_, row) => <div className="grid gap-2" style={{ overflowWrap: 'anywhere' }}><span>{row.annotation_reason}</span><span>{row.reason}</span></div> },
    { title: '操作', key: 'actions', width: 130, responsivePriority: 'always', responsiveAction: true, render: (_, row) => row.status === 'review' && row.property_id && row.community_id && row.manual_confirm_item ? <Button size="small" onClick={() => void openManualConfirm(row)} disabled={busy || applyFailed}>确认小区</Button> : null },
  ]
  const close = () => { if (!busy) setOpen(false) }
  return <>
    <Button icon={<UploadOutlined />} onClick={() => { setOpen(true); setPreview(null); setSelected([]); setError(''); setApplyFailed(false); setStatusFilter('all') }}>回导小区标注</Button>
    <Modal title="小区标注回导预览" open={open} onCancel={close} width={1100} centered
      style={{ maxWidth: 'calc(100vw - 32px)' }} maskClosable={!busy} closable={!busy}
      styles={{ body: { maxHeight: layout.height - 170, overflowY: 'auto' } }}
      footer={<div className="flex flex-wrap justify-end gap-3">
        <Button disabled={busy} onClick={close}>关闭</Button>
        <Button type="primary" loading={busy} disabled={!selectedRows.length || applyFailed} onClick={() => Modal.confirm({
          title: `确认应用 ${selectedRows.length} 套房屋的标注？`,
          content: replacementCount ? `其中 ${replacementCount} 套将替换已有人工确认，请核对标注依据和所选小区。` : '只更新所选房屋的小区关联；原始地址和社区保持不变。',
          okText: '确认应用', cancelText: '取消', onOk: apply,
        })}>确认所选 {selectedRows.length} 条</Button>
      </div>}>
      <div className="grid gap-4">
        <Upload accept=".xlsx" showUploadList={false} disabled={busy} beforeUpload={file => {
          if (file.size > 100 * 1024 * 1024) { setError('标注文件超过 100MB'); return false }
          void read(file); return false
        }}><Button icon={<UploadOutlined />} disabled={busy}>选择已标注工作簿并预览</Button></Upload>
        {error && <Alert type="error" showIcon message={error} />}
        {preview && <>
          <div className="flex flex-wrap items-center gap-3">
            <span>共 {preview.total} 条</span><span>可确认 {counts.ready}</span><span>拒绝应用 {counts.blocked}</span><span>待复核 {counts.review}</span><span>跳过 {counts.skipped}</span>{counts.applied > 0 && <span>已应用 {counts.applied}</span>}
            <Select aria-label="标注状态筛选" value={statusFilter} onChange={value => setStatusFilter(value as PreviewStatusFilter)} options={statusOptions} style={{ minWidth: 170 }} />
          </div>
          {preview.items.some(row => row.replaces_manual) && <Alert type="warning" showIcon message="替换已有人工确认的记录未默认勾选，请逐条核对后选择" />}
          <AppTable rowKey="xlsx_row" columns={columns} dataSource={visibleItems} loading={busy} scroll={{ x: 1000, y: Math.max(120, Math.min(360, layout.height - 430)) }}
            pagination={{ pageSize: 20, showSizeChanger: false }} rowSelection={{
              selectedRowKeys: selected, preserveSelectedRowKeys: true,
              onChange: keys => setSelected(keys.map(Number)),
              getCheckboxProps: row => ({ disabled: busy || applyFailed || row.status !== 'ready' }),
            }} />
        </>}
      </div>
    </Modal>
    <Modal title="人工确认小区" open={Boolean(manualRow)} centered onCancel={() => { if (!manualSaving) setManualRow(null) }} onOk={() => void confirmManual()} okText="确认" cancelText="取消" confirmLoading={manualSaving} okButtonProps={{ disabled: !manualSmallCommunityId || manualLoading }} destroyOnClose>
      <div className="grid gap-3">
        <div>房屋地址：{manualRow?.address || '无可查看地址'}</div>
        <div>所属社区：{manualRow?.community || '未识别'}</div>
        <Select showSearch optionFilterProp="label" placeholder="请选择确认的小区" value={manualSmallCommunityId} loading={manualLoading} onChange={setManualSmallCommunityId} options={manualOptions.map(option => ({ value: option.id, label: option.detail_address ? `${option.name}（${option.detail_address}）` : option.name }))} />
      </div>
    </Modal>
  </>
}
