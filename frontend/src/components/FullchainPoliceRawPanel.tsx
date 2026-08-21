import { useEffect, useState } from 'react'
import { Alert, Button, Modal, Statistic, Table, Tag, Upload, message } from 'antd'
import type { UploadFile, UploadProps } from 'antd'
import { InboxOutlined, UploadOutlined } from '@ant-design/icons'
import {
  apiErrorMessage,
  confirmFullchainPoliceRaw,
  formatUTCTime,
  listFullchainPoliceRawUploads,
  fullchainPoliceRawDownloadUrl,
  previewFullchainPoliceRaw,
} from '../api/client'
import { useAuth } from '../context/AuthContext'
import { Panel } from './ui'

const { Dragger } = Upload

export default function FullchainPoliceRawPanel({ enabled }: { enabled: boolean }) {
  const { systemTimezone } = useAuth()
  const [file, setFile] = useState<File | null>(null)
  const [fileList, setFileList] = useState<UploadFile[]>([])
  const [preview, setPreview] = useState<Awaited<ReturnType<typeof previewFullchainPoliceRaw>> | null>(null)
  const [history, setHistory] = useState<Awaited<ReturnType<typeof listFullchainPoliceRawUploads>>['data']>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const loadHistory = async () => {
    if (!enabled) return
    try { setHistory((await listFullchainPoliceRawUploads()).data) } catch { /* 不阻断当前上传 */ }
  }
  useEffect(() => { void loadHistory() }, [enabled])

  const beforeUpload: UploadProps['beforeUpload'] = selected => {
    if (!/\.xlsx?$/i.test(selected.name)) {
      message.error('只支持 .xls 或 .xlsx 文件')
      return Upload.LIST_IGNORE
    }
    setFile(selected)
    setPreview(null)
    setFileList([{ uid: selected.uid, name: selected.name, size: selected.size, status: 'done', originFileObj: selected }])
    return false
  }

  const handlePreview = async () => {
    if (!file) return
    setLoading(true); setError('')
    try { setPreview(await previewFullchainPoliceRaw(file)) }
    catch (reason: unknown) { setError(apiErrorMessage(reason, '公安网原始数据预览失败')) }
    finally { setLoading(false) }
  }

  const handleConfirm = () => {
    if (!file || !preview) return
    Modal.confirm({
      title: '确认替换当前公安网原始数据？',
      content: `确认后将用这 ${preview.row_count} 条记录判断“已登记”任务是否仍在公安网原始数据中。历史上传记录仍保留。`,
      okText: '确认使用本次数据', cancelText: '取消',
      onOk: async () => {
        setLoading(true); setError('')
        try {
          const result = await confirmFullchainPoliceRaw(file, preview.preview_token)
          message.success(result.message)
          setFile(null); setFileList([]); setPreview(null)
          await loadHistory()
        } catch (reason: unknown) { setError(apiErrorMessage(reason, '公安网原始数据确认失败')) }
        finally { setLoading(false) }
      },
    })
  }

  return (
    <Panel title="公安网原始数据" description="仅用于全链条“已登记”数据比对。先预览，确认后才替换当前比对快照；历史上传记录永久可查。">
      <div className="grid gap-4">
        <Alert type="info" showIcon message="判断规则" description="当前核查结果为“已登记”，且身份证号在最近一次确认的公安网原始数据中已不存在时，才进入反馈归档候选。" />
        <Dragger accept=".xls,.xlsx" maxCount={1} fileList={fileList} beforeUpload={beforeUpload} disabled={!enabled || loading} onRemove={() => { setFile(null); setFileList([]); setPreview(null) }}>
          <p className="ant-upload-drag-icon"><InboxOutlined /></p>
          <p className="ant-upload-text">拖入公安网原始数据，或点击选择</p>
          <p className="ant-upload-hint">系统只保存用于精确匹配的身份证摘要，不在任务日志中保存身份证号。</p>
        </Dragger>
        <div className="flex flex-wrap justify-end gap-2">
          <Button type="primary" icon={<UploadOutlined />} disabled={!file || loading} loading={loading} onClick={() => void handlePreview()}>预览原始数据</Button>
          {preview && <Button type="primary" disabled={loading} onClick={handleConfirm}>确认作为当前比对数据</Button>}
        </div>
        {error && <Alert type="error" showIcon message={error} />}
        {preview && <>
          <div className="grid gap-3 sm:grid-cols-3">
            <Statistic title="有效记录" value={preview.row_count} suffix="条" />
            <Statistic title="重复身份证" value={preview.duplicate_count} suffix="条" />
            <Statistic title="无效记录" value={preview.invalid_count} suffix="条" />
          </div>
          <Table size="small" rowKey="row" dataSource={preview.preview} pagination={false} columns={[
            { title: '原文件行', dataIndex: 'row', width: 100 },
            { title: '姓名', dataIndex: 'name', width: 120 },
            { title: '身份证号', dataIndex: 'identity', width: 220 },
            { title: '原结果', dataIndex: 'result' },
          ]} />
        </>}
        {history.length > 0 && <Table size="small" rowKey="id" dataSource={history} pagination={false} columns={[
          { title: '文件', dataIndex: 'file_name', ellipsis: true },
          { title: '记录', dataIndex: 'row_count', width: 90 },
          { title: '状态', dataIndex: 'status', width: 100, render: value => <Tag color={value === 'confirmed' ? 'green' : 'default'}>{value === 'confirmed' ? '当前使用' : '历史快照'}</Tag> },
          { title: '上传时间', dataIndex: 'created_at', width: 190, render: value => formatUTCTime(value, systemTimezone) },
          { title: '原始文件', width: 100, render: (_: unknown, item) => <Button type="link" href={fullchainPoliceRawDownloadUrl(item.id)}>下载</Button> },
        ]} />}
      </div>
    </Panel>
  )
}
