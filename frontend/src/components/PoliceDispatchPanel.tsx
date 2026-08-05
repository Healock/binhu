import { useEffect, useState } from 'react'
import { Alert, Button, Input, Progress, Select, Space, Statistic, Tag, Upload, message } from 'antd'
import type { TableColumnsType, UploadFile, UploadProps } from 'antd'
import { ExportOutlined, InboxOutlined, RightOutlined, UploadOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import AppTable from './AppTable'
import { Panel } from './ui'
import {
  listPoliceDispatchBatches,
  policeDispatchFeedbackUrl,
  uploadPoliceDispatchBatch,
  type PoliceDispatchBatch,
} from '../api/client'

const { Dragger } = Upload

const statusMeta: Record<string, { color: string; text: string }> = {
  reviewing: { color: 'processing', text: '审核中' },
  ready_to_publish: { color: 'warning', text: '待发布' },
  publishing: { color: 'blue', text: '发布中' },
  reconciling: { color: 'error', text: '待对账/冲突' },
  completed: { color: 'success', text: '已完成' },
}

export default function PoliceDispatchPanel({ enabled }: { enabled: boolean }) {
  const navigate = useNavigate()
  const [file, setFile] = useState<File | null>(null)
  const [fileList, setFileList] = useState<UploadFile[]>([])
  const [batches, setBatches] = useState<PoliceDispatchBatch[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [fileName, setFileName] = useState('')
  const [uploadDate, setUploadDate] = useState('')
  const [status, setStatus] = useState('all')
  const [loading, setLoading] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')

  const load = async (targetPage = page) => {
    if (!enabled) return
    setLoading(true)
    try {
      const result = await listPoliceDispatchBatches({
        file_name: fileName || undefined,
        upload_date: uploadDate || undefined,
        status,
        page: targetPage,
        page_size: 20,
      })
      setBatches(result.data)
      setTotal(result.total)
      setPage(targetPage)
      setError('')
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '下发批次读取失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load(1) }, [enabled, status])

  const beforeUpload: UploadProps['beforeUpload'] = selected => {
    const lower = selected.name.toLowerCase()
    if (!lower.endsWith('.xls') && !lower.endsWith('.xlsx')) {
      message.error('只支持 .xls 或 .xlsx 文件')
      return Upload.LIST_IGNORE
    }
    setFile(selected)
    setFileList([{
      uid: selected.uid,
      name: selected.name,
      size: selected.size,
      status: 'done',
      originFileObj: selected,
    }])
    return false
  }

  const upload = async () => {
    if (!file) return
    setUploading(true)
    try {
      const result = await uploadPoliceDispatchBatch(file)
      message.success(result.message)
      setFile(null)
      setFileList([])
      await load(1)
      navigate(`/police-dispatch/batches/${result.batch.id}`)
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '数据导入失败')
    } finally {
      setUploading(false)
    }
  }

  const latest = batches[0]
  const columns: TableColumnsType<PoliceDispatchBatch> = [
    { title: '批次', dataIndex: 'id', width: 80, render: value => `#${value}` },
    { title: '原文件', dataIndex: 'file_name', ellipsis: true, width: 260 },
    {
      title: '审核进度', width: 190,
      render: (_, item) => (
        <Progress
          size="small"
          percent={item.total_count ? Math.round(item.reviewed_count / item.total_count * 100) : 0}
          format={() => `${item.reviewed_count}/${item.total_count}`}
        />
      ),
    },
    {
      title: '状态', dataIndex: 'status', width: 100,
      render: value => <Tag color={statusMeta[value]?.color}>{statusMeta[value]?.text || value}</Tag>,
    },
    {
      title: '操作', width: 220,
      render: (_, item) => (
        <Space>
          <Button type="link" onClick={() => navigate(`/police-dispatch/batches/${item.id}`)}>
            查看批次 <RightOutlined />
          </Button>
          <Button
            type="link"
            icon={<ExportOutlined />}
            href={policeDispatchFeedbackUrl(item.id)}
          >
            反馈 XLSX
          </Button>
        </Space>
      ),
    },
  ]

  return (
    <Panel
      title="数据下发"
      description="导入全链条系统导出文件；平台只提供预处理建议，所有记录仍须基础管控或中队长人工审核"
      padded={false}
    >
      <div className="space-y-5 p-5">
        {!enabled && (
          <Alert type="info" showIcon message="当前账号没有数据下发权限" />
        )}
        {error && <Alert type="error" showIcon message={error} closable onClose={() => setError('')} />}
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center">
          <Dragger
            accept=".xls,.xlsx"
            maxCount={1}
            fileList={fileList}
            beforeUpload={beforeUpload}
            onRemove={() => { setFile(null); setFileList([]) }}
            disabled={!enabled || uploading}
          >
            <p className="ant-upload-drag-icon"><InboxOutlined /></p>
            <p className="ant-upload-text">拖入全链条系统导出文件，或点击选择</p>
            <p className="ant-upload-hint">支持 .xls/.xlsx；身份证号、手机号和日期始终按文本保存</p>
          </Dragger>
          <Button
            type="primary"
            size="large"
            icon={<UploadOutlined />}
            disabled={!enabled || !file}
            loading={uploading}
            onClick={upload}
          >
            导入并生成审核任务
          </Button>
        </div>

        {latest && (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-8">
            {[
              ['总数', latest.counts.total],
              ['待审核', latest.counts.pending_review],
              ['无需登记', latest.counts.no_registration],
              ['移交', latest.counts.transfer],
              ['社区下发', latest.counts.dispatch],
              ['模糊平均分配', latest.counts.balanced],
              ['重复记录', latest.counts.duplicate],
              ['异常判断', latest.counts.abnormal],
            ].map(([label, value]) => (
              <div key={String(label)} className="rounded-xl border border-slate-200 bg-slate-50/60 p-3">
                <Statistic title={label} value={value} suffix="条" valueStyle={{ fontSize: 22 }} />
              </div>
            ))}
          </div>
        )}

        {latest?.community_distribution?.length > 0 && (
          <div>
            <div className="mb-2 text-sm font-medium text-slate-700">最近批次社区分配</div>
            <div className="flex flex-wrap gap-2">
              {latest.community_distribution.map(item => (
                <Tag key={item.community_id} color="blue">{item.community_name} {item.count} 条</Tag>
              ))}
            </div>
          </div>
        )}

        <div className="grid gap-2 md:grid-cols-[minmax(0,1fr)_180px_170px_auto]">
          <Input.Search
            allowClear
            value={fileName}
            placeholder="按原文件名筛选"
            onChange={event => setFileName(event.target.value)}
            onSearch={() => void load(1)}
          />
          <Input
            type="date"
            value={uploadDate}
            onChange={event => setUploadDate(event.target.value)}
          />
          <Select
            value={status}
            onChange={setStatus}
            options={[
              { value: 'all', label: '全部状态' },
              { value: 'reviewing', label: '审核中' },
              { value: 'ready_to_publish', label: '待发布' },
              { value: 'reconciling', label: '待对账/冲突' },
              { value: 'completed', label: '已完成' },
            ]}
          />
          <Button onClick={() => void load(1)}>筛选</Button>
        </div>

        <AppTable<PoliceDispatchBatch>
          rowKey="id"
          columns={columns}
          dataSource={batches.slice(0, 8)}
          loading={loading}
          pagination={{
            current: page,
            pageSize: 20,
            total,
            showSizeChanger: false,
            onChange: nextPage => void load(nextPage),
          }}
          scroll={{ x: 850 }}
          size="small"
        />
      </div>
    </Panel>
  )
}
