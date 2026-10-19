import { DeleteOutlined, UploadOutlined } from '@ant-design/icons'
import { Button, Upload, message } from 'antd'

export default function HouseholdImportFiles({ file, busy, onChange }: {
  file: File | null
  busy: boolean
  onChange: (file: File | null) => void
}) {
  return <section className="grid gap-3" aria-label="户号表文件">
    <Upload accept=".xlsx" showUploadList={false} disabled={busy} beforeUpload={nextFile => {
      if (!nextFile.name.toLowerCase().endsWith('.xlsx')) { message.error('请选择 XLSX 户号表'); return false }
      if (nextFile.size > 50 * 1024 * 1024) { message.error('户号表不能超过 50MB'); return false }
      onChange(nextFile)
      return false
    }}><Button icon={<UploadOutlined />} disabled={busy}>选择标准户号表</Button></Upload>
    {file && <div className="flex items-center justify-between gap-3 rounded border border-[var(--app-border)] px-3 py-2">
      <span style={{ overflowWrap: 'anywhere' }}>{file.name}</span>
      <Button aria-label={`移除${file.name}`} title="移除文件" icon={<DeleteOutlined />} disabled={busy} onClick={() => onChange(null)} />
    </div>}
  </section>
}
