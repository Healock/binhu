import { useEffect, useState } from 'react'
import { Alert, Button, Input, Popconfirm, Progress, Select, Segmented, Space, Statistic, Tag, Upload, message } from 'antd'
import type { TableColumnsType, UploadFile, UploadProps } from 'antd'
import { ExportOutlined, InboxOutlined, RightOutlined, SendOutlined, UploadOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import AppTable from './AppTable'
import { Panel } from './ui'
import {
  listPoliceDispatchBatches,
  policeDispatchFeedbackUrl,
  publishPoliceDispatchBatch,
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
  const [publishingBatchId, setPublishingBatchId] = useState<number | null>(null)
  const [importMode, setImportMode] = useState<'raw' | 'clean'>('raw')
  const [cleanPreview, setCleanPreview] = useState<NonNullable<Awaited<ReturnType<typeof uploadPoliceDispatchBatch>>['preview']> | null>(null)
  const [cleanPreviewToken, setCleanPreviewToken] = useState('')
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

  useEffect(() => {
    setCleanPreview(null)
    setCleanPreviewToken('')
  }, [importMode])

  const beforeUpload: UploadProps['beforeUpload'] = selected => {
    const lower = selected.name.toLowerCase()
    if (!lower.endsWith('.xls') && !lower.endsWith('.xlsx')) {
      message.error('只支持 .xls 或 .xlsx 文件')
      return Upload.LIST_IGNORE
    }
    setFile(selected)
    setCleanPreview(null)
    setCleanPreviewToken('')
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
      const result = await uploadPoliceDispatchBatch(file, importMode, {
        confirm: importMode === 'clean' && Boolean(cleanPreviewToken),
        previewToken: cleanPreviewToken || undefined,
      })
      if (result.status === 'preview') {
        setCleanPreview(result.preview || null)
        setCleanPreviewToken(result.preview_token || '')
        message.success('预览已生成，请核对摘要后再次点击确认导入')
        return
      }
      message.success(result.message)
      setFile(null)
      setFileList([])
      setCleanPreview(null)
      setCleanPreviewToken('')
      await load(1)
      if (result.batch) {
        navigate(
          result.batch.counts.pending_publish > 0
            ? `/police-tasks?batch=${result.batch.id}&status=pending_publish&category=all`
            : result.batch.counts.pending_review > 0
              ? `/police-tasks?batch=${result.batch.id}&status=pending_review&category=all`
              : `/police-dispatch/batches/${result.batch.id}`,
        )
      }
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '数据导入失败')
    } finally {
      setUploading(false)
    }
  }

  const publishBatch = async (batch: PoliceDispatchBatch) => {
    setPublishingBatchId(batch.id)
    try {
      const result = await publishPoliceDispatchBatch(batch.id)
      message[result.failed_count ? 'warning' : 'success'](result.message)
      await load(page)
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '整批发布失败')
    } finally {
      setPublishingBatchId(null)
    }
  }

  const publishableCount = (batch: PoliceDispatchBatch) => (
    batch.import_mode === 'clean' && batch.counts.pending_review > 0
      ? batch.counts.partial_publishable || 0
      : batch.counts.publishable || 0
  )

  const latest = batches[0]
  const columns: TableColumnsType<PoliceDispatchBatch> = [
    { title: '批次', dataIndex: 'id', width: 80, render: value => `#${value}` },
    { title: '原文件', dataIndex: 'file_name', ellipsis: true, width: 260 },
    {
      title: '导入类型', dataIndex: 'import_mode', width: 150,
      render: value => value === 'clean' ? <Tag color="green">已处理直发</Tag> : <Tag>原始审核</Tag>,
    },
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
      title: '操作', width: 330,
      render: (_, item) => (
        <Space>
          {publishableCount(item) > 0 && (
            <Popconfirm
              title={`整批发布 ${publishableCount(item)} 条已审核任务？`}
              description={item.counts.pending_review > 0
                ? `其余 ${item.counts.pending_review} 条待复核记录不会发布。`
                : '发布后将写入腾讯全链条表。'}
              okText="确认发布"
              cancelText="取消"
              onConfirm={() => publishBatch(item)}
            >
              <Button
                type="link"
                icon={<SendOutlined />}
                loading={publishingBatchId === item.id}
              >
                整批发布（{publishableCount(item)}）
              </Button>
            </Popconfirm>
          )}
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
  const previewColumns: TableColumnsType<NonNullable<typeof cleanPreview>['rows'][number]> = [
    { title: 'Excel 行', dataIndex: 'source_row', width: 90 },
    { title: '姓名', dataIndex: 'person_name', width: 110 },
    { title: '身份证号', dataIndex: 'identity_number', width: 190 },
    { title: '手机号', dataIndex: 'phone', width: 150 },
    { title: '社区', dataIndex: 'community_name', width: 130 },
    { title: '登记情况', dataIndex: 'registration_status', width: 150 },
    {
      title: '导入结果', dataIndex: 'result', width: 130,
      render: value => value === 'dispatch'
        ? <Tag color="blue">直接下发</Tag>
        : <Tag color="orange">人工复核</Tag>,
    },
    { title: '原因', dataIndex: 'reason', ellipsis: true, width: 280 },
  ]

  return (
    <Panel
      title="数据下发"
      description="原始数据按平台建议进入审核；基础管控已处理的数据可按登记情况直接生成待发布任务"
      padded={false}
    >
      <div className="space-y-5 p-5">
        {!enabled && (
          <Alert type="info" showIcon message="当前账号没有数据下发权限" />
        )}
        {error && <Alert type="error" showIcon message={error} closable onClose={() => setError('')} />}
        {cleanPreview && (
          <Alert
            type="info"
            showIcon
            message="已处理数据预览"
            description={`共 ${cleanPreview.row_count} 行：直接下发 ${cleanPreview.counts.dispatch} 行，需人工复核 ${cleanPreview.counts.manual_review} 行。登记情况会随任务下发，供网格员核查和重新登记时参考。`}
          />
        )}
        <div className="police-dispatch-upload-mode flex flex-wrap items-center gap-3">
          <span className="text-sm font-medium text-[var(--app-text-secondary)]">导入类型</span>
          <Segmented
            value={importMode}
            onChange={value => setImportMode(value as 'raw' | 'clean')}
            options={[
              { value: 'raw', label: '原始数据审核' },
              { value: 'clean', label: '已处理数据直接下发' },
            ]}
          />
          <span className="text-xs text-[var(--app-text-secondary)]">
            {importMode === 'clean'
              ? '需要社区、登记情况、姓名、身份证号、手机号和地址；异常行仍需人工处理'
              : '适用于全链条系统导出的待处理原始文件'}
          </span>
        </div>
        <div className="police-dispatch-upload-row grid gap-4 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center">
          <Dragger
            accept=".xls,.xlsx"
            maxCount={1}
            fileList={fileList}
            beforeUpload={beforeUpload}
            onRemove={() => {
              setFile(null)
              setFileList([])
              setCleanPreview(null)
              setCleanPreviewToken('')
            }}
            disabled={!enabled || uploading}
          >
            <p className="ant-upload-drag-icon"><InboxOutlined /></p>
            <p className="ant-upload-text">
              {importMode === 'clean' ? '拖入基础管控已处理文件，或点击选择' : '拖入全链条系统导出文件，或点击选择'}
            </p>
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
            {importMode === 'clean'
              ? (cleanPreviewToken ? '确认导入并进入待发布' : '先预览已处理数据')
              : '导入并生成审核任务'}
          </Button>
        </div>

        {cleanPreview && (
          <div className="space-y-3">
            <div className="flex flex-wrap gap-2">
              {cleanPreview.community_distribution.map(item => (
                <Tag key={item.community_id} color="blue">{item.community_name} {item.count} 条</Tag>
              ))}
            </div>
            <AppTable
              rowKey="source_row"
              columns={previewColumns}
              dataSource={cleanPreview.rows}
              pagination={false}
              scroll={{ x: 1200, y: 420 }}
              size="small"
            />
            {cleanPreview.rows_truncated && (
              <div className="text-xs text-[var(--app-text-secondary)]">明细仅展示前 100 行，顶部统计覆盖完整文件。</div>
            )}
          </div>
        )}

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
              ['待研判', latest.counts.abnormal],
            ].map(([label, value]) => (
              <div key={String(label)} className="police-dispatch-summary-card rounded-xl p-3">
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
