import { DeleteOutlined, UploadOutlined } from '@ant-design/icons'
import { Button, InputNumber, Select, Upload, message } from 'antd'
import AppTable from './AppTable'
import type { ResponsiveColumns } from './responsiveTable'
import type { HouseholdImportFile } from '../api/client'

export default function HouseholdImportFiles({ files, busy, onChange }: {
  files: HouseholdImportFile[]
  busy: boolean
  onChange: (update: (current: HouseholdImportFile[]) => HouseholdImportFile[]) => void
}) {
  const update = (key: string, changes: Partial<HouseholdImportFile>) => onChange(current => current.map(item => item.key === key ? { ...item, ...changes } : item))
  const columns: ResponsiveColumns<HouseholdImportFile> = [
    { title: '文件', key: 'file', width: 260, responsivePriority: 'always', render: (_, item) => <span style={{ overflowWrap: 'anywhere' }}>{item.file.name}</span> },
    { title: '查询住房类型', key: 'housing_type', width: 170, responsivePriority: 'always', render: (_, item) => <Select aria-label={`${item.file.name}住房类型`} disabled={busy} className="w-full" value={item.housing_type}
      options={[{ value: '', label: '按表内类型' }, ...['个人租赁', '单位租赁', '自购房屋', '借住', '其他'].map(value => ({ value, label: value }))]}
      onChange={value => update(item.key, { housing_type: value })} /> },
    { title: '查询注销状态', key: 'household_status', width: 170, responsivePriority: 'always', render: (_, item) => <Select aria-label={`${item.file.name}注销状态`} disabled={busy} className="w-full" value={item.household_status}
      options={[{ value: '', label: '按表内状态' }, { value: 'cancelled', label: '已注销（是）' }, { value: 'not_cancelled', label: '未注销（否）' }]}
      onChange={value => update(item.key, { household_status: value })} /> },
    { title: '内网查询总数', key: 'expected_count', width: 170, responsivePriority: 'always', render: (_, item) => <InputNumber aria-label={`${item.file.name}查询总数`} disabled={busy} className="w-full" min={0} max={100000} precision={0} value={item.expected_count} placeholder="待核对"
      onChange={value => update(item.key, { expected_count: value ?? undefined })} /> },
    { title: '操作', key: 'actions', width: 70, render: (_, item) => <Button aria-label={`移除${item.file.name}`} title="移除文件" icon={<DeleteOutlined />} disabled={busy} onClick={() => onChange(current => current.filter(row => row.key !== item.key))} /> },
  ]
  return <section className="grid gap-3" aria-label="户号表文件与查询条件">
    <Upload accept=".xlsx" multiple showUploadList={false} disabled={busy} beforeUpload={file => {
      onChange(current => {
        if (current.length >= 10) { message.error('每批最多 10 份户号表'); return current }
        if (!file.name.toLowerCase().endsWith('.xlsx')) { message.error('请选择 XLSX 户号表'); return current }
        if (current.reduce((total, item) => total + item.file.size, file.size) > 50 * 1024 * 1024) { message.error('本批文件总大小不能超过 50MB'); return current }
        return [...current, { key: file.uid, file, housing_type: '', household_status: '' }]
      })
      return false
    }}><Button icon={<UploadOutlined />} disabled={busy}>选择户号表（最多10份）</Button></Upload>
    {files.length > 0 && <AppTable rowKey="key" columns={columns} dataSource={files} pagination={false} scroll={{ x: 840 }} />}
  </section>
}
