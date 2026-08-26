import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Descriptions, Input, Progress, Select, Space, Statistic, Tag } from 'antd'
import type { TableColumnsType } from 'antd'
import { ExportOutlined, MobileOutlined, SearchOutlined } from '@ant-design/icons'
import { useNavigate, useParams } from 'react-router-dom'
import AppTable from '../components/AppTable'
import { ListContent, ListToolbar, PageHeader, Panel } from '../components/ui'
import type { ResponsiveColumns } from '../components/responsiveTable'
import useDebouncedValue from '../hooks/useDebouncedValue'
import {
  getPoliceDispatchBatch,
  listPoliceDispatchTasks,
  policeDispatchFeedbackUrl,
  type PoliceDispatchBatch,
  type PoliceDispatchTask,
} from '../api/client'

const actionLabels: Record<string, string> = {
  dispatch: '下发到社区',
  no_registration: '无需登记',
  transfer: '移交',
  duplicate_exclude: '重复排除',
  manual: '待研判',
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

const businessLabels: Record<string, string> = {
  fullchain: '全链条', rental: '出租房屋核查', police: '涉警',
  delivery: '寄递业', suspect_return: '疑似返苏',
}
const policeSubtypeLabels: Record<string, string> = {
  internal: '所内涉警',
  suzhou: '苏州涉警',
  traffic: '交通涉警',
}

function standardValue(task: PoliceDispatchTask, ...keys: string[]): string {
  for (const key of keys) {
    const value = task.standard_values?.[key]
    if (value) return String(value)
  }
  return ''
}

function taskTitle(task: PoliceDispatchTask): string {
  if (task.police_subtype === 'internal' || task.target_parser === '涉警统计') {
    return standardValue(task, '序号', '接警编号') || `第 ${task.source_row} 行`
  }
  return task.person_name || standardValue(task, '姓名') || `第 ${task.source_row} 行`
}

function taskDetailItems(task: PoliceDispatchTask) {
  const values = task.standard_values || {}
  const preferred = task.target_parser === '涉警统计'
    ? ['序号', '日期', '社区', '简要警情及处理结果']
    : task.target_parser === '苏州涉警'
      ? ['下发日期', '截止日期', '核查人', '社区', '姓名', '身份证号', '联系号码', '疑似现住址', '接警编号', '出警日期', '出警类别', '出警内容', '出警单位', '参考派出所', '现住址', '核查结果', '研判', '二次反馈']
      : task.target_parser === '交通涉警'
        ? ['下发日期', '截止日期', '核查人', '社区', '姓名', '身份证号', '联系号码', '地址1', '现住址', '核查结果', '研判', '二次反馈']
      : task.target_parser === '寄递业'
        ? ['下发时间', '截止时间', '核查人', '姓名', '身份证号', '地址1', '手机号码', '社区', '参考姓名', '参考身份证号码', '现住址', '核查结果', '研判', '二次反馈']
        : task.target_parser === '疑似返苏'
          ? ['下发日期', '截止日期', '核查人', '业务分类', '姓名', '身份证号码', '联系号码', '高频抓拍小区', '现住址', '核查反馈', '研判', '二次核查结果']
          : ['下发日期', '截止日期', '核查人', '社区', '姓名', '身份证号', '电话号码', '地址', '登记情况', '现住址', '核查结果', '研判', '二次反馈']
  return preferred.filter(key => values[key] !== undefined && String(values[key] ?? '').trim() !== '').map(key => ({
    key,
    label: key,
    children: String(values[key]),
  }))
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
  const [keywordFlush, setKeywordFlush] = useState(0)
  const keyword = useDebouncedValue(keywordInput.trim(), 350, keywordFlush)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const listRequestId = useRef(0)

  const load = async (targetPage = page, targetPageSize = pageSize) => {
    if (!id) return
    const requestId = ++listRequestId.current
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
      if (requestId !== listRequestId.current) return
      setBatch(batchResult.batch)
      setTasks(taskResult.data)
      setTotal(taskResult.total)
      setPage(targetPage)
      setPageSize(targetPageSize)
      setError('')
    } catch (reason: any) {
      if (requestId === listRequestId.current) setError(reason?.response?.data?.detail || '批次读取失败')
    } finally {
      if (requestId === listRequestId.current) setLoading(false)
    }
  }

  useEffect(() => { void load(1, 20) }, [id, status, category, keyword])

  const columns: ResponsiveColumns<PoliceDispatchTask> = [
    { title: 'Excel 行', dataIndex: 'source_row', width: 90, responsivePriority: 'standard' },
    { title: batch?.target_parser === '涉警统计' ? '接警编号' : '姓名', width: 150, responsivePriority: 'always', render: (_, item) => taskTitle(item) },
    { title: '身份证号', width: 190, responsivePriority: 'standard', render: (_, item) => item.identity_number || standardValue(item, '身份证号', '身份证号码') || '—' },
    { title: '手机号', width: 150, responsivePriority: 'standard', render: (_, item) => item.phone || standardValue(item, '电话号码', '手机号码', '联系号码') || '—' },
    { title: batch?.target_parser === '涉警统计' ? '简要警情及处理结果' : '原地址', width: 340, responsivePriority: 'always', ellipsis: true, render: (_, item) => item.original_address || standardValue(item, '疑似现住址', '地址1', '高频抓拍小区', '简要警情及处理结果') || '—' },
    { title: '社区', width: 130, responsivePriority: 'standard', render: (_, item) => standardValue(item, '社区', '业务分类') || '—' },
    {
      title: '建议', width: 150, responsivePriority: 'standard',
      render: (_, item) => (
        <span>{actionLabels[item.suggested_action]}{item.suggested_community_name ? ` · ${item.suggested_community_name}` : ''}</span>
      ),
    },
    {
      title: '最终结果', width: 170, responsivePriority: 'always',
      render: (_, item) => item.final_action
        ? <Tag color={item.task_status === 'completed' ? 'success' : 'processing'}>
            {actionLabels[item.final_action]}{item.final_community_name ? ` · ${item.final_community_name}` : ''}
          </Tag>
        : <Tag>待审核</Tag>,
    },
    {
      title: '异常', width: 120, responsivePriority: 'standard',
      render: (_, item) => (
        <Space direction="vertical" size={2}>
          {item.duplicate_group_key && <Tag color="orange">{item.duplicate_kind === 'exact' ? '完全重复' : '重复有差异'}</Tag>}
          {item.allocation_mode === 'balanced' && <Tag color="blue">平均分配</Tag>}
          {item.suggested_action === 'manual' && <Tag color="red">待研判</Tag>}
        </Space>
      ),
    },
    {
      title: '发布状态', width: 180, responsivePriority: 'standard',
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
        title={batch ? `${businessLabels[batch.business_type] || batch.target_parser}${batch.police_subtype ? ` · ${policeSubtypeLabels[batch.police_subtype] || batch.police_subtype}` : ''} · 批次 #${batch.id}` : '下发批次'}
        description={batch
          ? `${batch.file_name} · ${batch.import_mode === 'quick' ? '快捷下发' : batch.import_mode === 'clean' ? '已处理直发' : '原始审核'} · 目标解析器：${batch.target_parser} · 用于历史倒查、复盘和发布异常处理`
          : '查看审核进度、社区分配和腾讯发布结果'}
        actions={batch && (
          <Space wrap>
            <Button icon={<MobileOutlined />} onClick={() => navigate(`/police-tasks?batch=${id}`)}>
              打开审核工作台
            </Button>
            <Button icon={<ExportOutlined />} href={policeDispatchFeedbackUrl(id)}>
              导出反馈 XLSX
            </Button>
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
        <ListContent inset>
          <ListToolbar
          filters={<>
          <Input
            allowClear
            prefix={<SearchOutlined />}
            value={keywordInput}
            placeholder="姓名、身份证号、手机号或地址"
            onChange={event => setKeywordInput(event.target.value)}
            onPressEnter={() => setKeywordFlush(current => current + 1)}
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
              { value: 'manual', label: '待研判' },
            ]}
          />
          </>}
          meta={<span>当前筛选 {total} 条</span>}
        />
        <AppTable<PoliceDispatchTask>
          fitHeight
          responsiveDetails
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
          expandable={{
            expandedRowRender: item => <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 3 }} items={taskDetailItems(item)} />,
          }}
        />
        </ListContent>
      </Panel>
    </div>
  )
}
