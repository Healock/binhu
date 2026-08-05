import { useEffect, useState } from 'react'
import { Alert, Button, Input, Popconfirm, Progress, Select, Space, Statistic, Tag, message } from 'antd'
import type { TableColumnsType } from 'antd'
import { ExportOutlined, MobileOutlined, SendOutlined } from '@ant-design/icons'
import { useNavigate, useParams } from 'react-router-dom'
import AppTable from '../components/AppTable'
import { PageHeader, Panel } from '../components/ui'
import {
  getPoliceDispatchBatch,
  listPoliceDispatchTasks,
  policeDispatchFeedbackUrl,
  publishPoliceDispatchBatch,
  type PoliceDispatchBatch,
  type PoliceDispatchTask,
} from '../api/client'

const actionLabels: Record<string, string> = {
  dispatch: '下发到社区',
  no_registration: '无需登记',
  transfer: '移交',
  duplicate_exclude: '重复排除',
  manual: '人工判断',
  '': '待审核',
}

const publishStatusLabels: Record<string, string> = {
  pending: '待发布',
  publishing: '发布中',
  retryable: '可安全重试',
  needs_reconciliation: '等待同步对账',
  conflict: '内容冲突',
  success: '发布成功',
  not_required: '不需发布',
}

export default function PoliceDispatchBatchDetail() {
  const { batchId } = useParams()
  const id = Number(batchId)
  const navigate = useNavigate()
  const [batch, setBatch] = useState<PoliceDispatchBatch | null>(null)
  const [tasks, setTasks] = useState<PoliceDispatchTask[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [status, setStatus] = useState('all')
  const [category, setCategory] = useState('all')
  const [keywordInput, setKeywordInput] = useState('')
  const [keyword, setKeyword] = useState('')
  const [loading, setLoading] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const [error, setError] = useState('')

  const load = async (targetPage = page, targetPageSize = pageSize) => {
    if (!id) return
    setLoading(true)
    try {
      const [batchResult, taskResult] = await Promise.all([
        getPoliceDispatchBatch(id),
        listPoliceDispatchTasks({
          batch_id: id,
          status,
          category,
          keyword,
          page: targetPage,
          page_size: targetPageSize,
        }),
      ])
      setBatch(batchResult.batch)
      setTasks(taskResult.data)
      setTotal(taskResult.total)
      setPage(targetPage)
      setPageSize(targetPageSize)
      setError('')
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '批次读取失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load(1, 20) }, [id, status, category, keyword])

  const publish = async () => {
    setPublishing(true)
    try {
      const result = await publishPoliceDispatchBatch(id)
      message[result.failed_count ? 'warning' : 'success'](result.message)
      await load(page, pageSize)
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '发布失败')
    } finally {
      setPublishing(false)
    }
  }

  const columns: TableColumnsType<PoliceDispatchTask> = [
    { title: 'Excel 行', dataIndex: 'source_row', width: 90 },
    { title: '姓名', dataIndex: 'person_name', width: 110 },
    { title: '身份证号', dataIndex: 'identity_number', width: 190 },
    { title: '手机号', dataIndex: 'phone', width: 150 },
    { title: '原地址', dataIndex: 'original_address', width: 340, ellipsis: true },
    {
      title: '建议', width: 150,
      render: (_, item) => (
        <span>{actionLabels[item.suggested_action]}{item.suggested_community_name ? ` · ${item.suggested_community_name}` : ''}</span>
      ),
    },
    {
      title: '最终结果', width: 170,
      render: (_, item) => item.final_action
        ? <Tag color={item.task_status === 'completed' ? 'success' : 'processing'}>
            {actionLabels[item.final_action]}{item.final_community_name ? ` · ${item.final_community_name}` : ''}
          </Tag>
        : <Tag>待审核</Tag>,
    },
    {
      title: '异常', width: 120,
      render: (_, item) => (
        <Space direction="vertical" size={2}>
          {item.duplicate_group_key && <Tag color="orange">{item.duplicate_kind === 'exact' ? '完全重复' : '重复有差异'}</Tag>}
          {item.allocation_mode === 'balanced' && <Tag color="blue">平均分配</Tag>}
          {item.suggested_action === 'manual' && <Tag color="red">人工判断</Tag>}
        </Space>
      ),
    },
    {
      title: '发布状态', width: 180,
      render: (_, item) => (
        <Space direction="vertical" size={2}>
          <Tag color={item.publish_status === 'success' ? 'success' : item.publish_status === 'conflict' ? 'error' : item.publish_status === 'needs_reconciliation' ? 'warning' : 'default'}>
            {publishStatusLabels[item.publish_status] || item.publish_status}
          </Tag>
          {item.cache_pending && <Tag color="blue">缓存待同步</Tag>}
          {item.publish_error && <span className="max-w-48 text-xs text-red-600">{item.publish_error}</span>}
        </Space>
      ),
    },
  ]

  return (
    <div className="app-page min-w-0">
      <PageHeader
        title={batch ? `公安下发批次 #${batch.id}` : '公安下发批次'}
        description={batch?.file_name || '查看审核进度、社区分配和腾讯发布结果'}
        extra={batch && (
          <Space wrap>
            <Button icon={<MobileOutlined />} onClick={() => navigate(`/police-tasks?batch=${id}`)}>
              打开审核工作台
            </Button>
            <Button icon={<ExportOutlined />} href={policeDispatchFeedbackUrl(id)}>
              导出反馈 XLSX
            </Button>
            <Popconfirm
              title="发布所有已审核的社区任务？"
              description="全批必须审核完成；成功后写入腾讯全链条表，不自动同步汇总。"
              okText="确认发布"
              cancelText="取消"
              onConfirm={publish}
            >
              <Button
                type="primary"
                icon={<SendOutlined />}
                loading={publishing}
                disabled={!batch || batch.counts.pending_review > 0 || (
                  batch.counts.pending_publish
                  - batch.counts.needs_reconciliation
                  - batch.counts.conflict
                ) <= 0}
              >
                发布到腾讯表格
              </Button>
            </Popconfirm>
          </Space>
        )}
      />
      {error && <Alert type="error" showIcon message={error} />}
      {batch && (
        <>
          {batch.last_error && <Alert className="mb-4" type="warning" showIcon message={batch.last_error} />}
          <Panel title="批次进度">
            <Progress
              percent={batch.total_count ? Math.round(batch.reviewed_count / batch.total_count * 100) : 0}
              format={() => `${batch.reviewed_count}/${batch.total_count} 已审核`}
            />
            <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-8">
              {[
                ['总数', batch.counts.total], ['待审核', batch.counts.pending_review],
                ['无需登记', batch.counts.no_registration], ['移交', batch.counts.transfer],
                ['社区下发', batch.counts.dispatch], ['平均分配', batch.counts.balanced],
                ['重复', batch.counts.duplicate], ['已发布', batch.counts.published],
              ].map(([label, value]) => (
                <div key={String(label)} className="rounded-xl border border-slate-200 p-3">
                  <Statistic title={label} value={value} suffix="条" valueStyle={{ fontSize: 22 }} />
                </div>
              ))}
            </div>
            <div className="mt-4 flex flex-wrap gap-2">
              {batch.community_distribution.map(item => (
                <Tag key={item.community_id} color="blue">{item.community_name} {item.count} 条</Tag>
              ))}
            </div>
          </Panel>
        </>
      )}
      <Panel title={`任务明细（${total}）`} description="逐条展示发布错误、待对账和内容冲突；敏感关键词通过请求体提交" padded={false}>
        <div className="grid gap-2 border-b border-slate-200 p-4 md:grid-cols-[minmax(0,1fr)_170px_170px_auto]">
          <Input.Search
            allowClear
            value={keywordInput}
            placeholder="姓名、身份证号、手机号或地址"
            onChange={event => setKeywordInput(event.target.value)}
            onSearch={() => setKeyword(keywordInput.trim())}
          />
          <Select
            value={status}
            onChange={setStatus}
            options={[
              { value: 'all', label: '全部状态' },
              { value: 'pending_review', label: '待审核' },
              { value: 'pending_publish', label: '待发布' },
              { value: 'retryable', label: '可安全重试' },
              { value: 'needs_reconciliation', label: '等待同步对账' },
              { value: 'conflict', label: '内容冲突' },
              { value: 'completed', label: '已完成' },
            ]}
          />
          <Select
            value={category}
            onChange={setCategory}
            options={[
              { value: 'all', label: '全部分类' },
              { value: 'dispatch', label: '社区下发' },
              { value: 'no_registration', label: '无需登记' },
              { value: 'transfer', label: '移交' },
              { value: 'balanced', label: '模糊分配' },
              { value: 'duplicate', label: '重复' },
              { value: 'manual', label: '人工判断' },
            ]}
          />
          <Button onClick={() => setKeyword(keywordInput.trim())}>查询</Button>
        </div>
        <AppTable<PoliceDispatchTask>
          rowKey="id"
          columns={columns}
          dataSource={tasks}
          loading={loading}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            onChange: (nextPage, nextPageSize) => void load(nextPage, nextPageSize),
          }}
          scroll={{ x: 1450 }}
        />
      </Panel>
    </div>
  )
}
