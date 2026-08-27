import { useCallback, useEffect, useMemo, useRef, useState, type Key } from 'react'
import { Alert, Button, Input, Modal, Progress, Select, Table, Tag, message } from 'antd'
import type { TableColumnsType } from 'antd'
import {
  apiErrorMessage,
  createFullchainArchiveExport,
  formatUTCTime,
  fullchainArchiveDownloadUrl,
  listFullchainArchiveExports,
  previewFullchainArchiveExport,
  saveFullchainArchiveReview,
  searchFullchainArchiveCandidates,
  selectFullchainArchiveCandidates,
  type FullchainArchiveCandidate,
  type FullchainArchiveExport,
} from '../api/client'
import { useAuth } from '../context/AuthContext'
import useDebouncedValue from '../hooks/useDebouncedValue'
import { Panel } from './ui'

const STAGES = [
  { value: 'direct', label: '可直接反馈' },
  { value: 'review', label: '待基础管控审核' },
  { value: 'registered', label: '已登记比对' },
] as const

interface FullchainArchivePanelProps {
  parserType?: string
}

export default function FullchainArchivePanel({ parserType = '全链条' }: FullchainArchivePanelProps) {
  const { systemTimezone } = useAuth()
  const [rows, setRows] = useState<FullchainArchiveCandidate[]>([])
  const [exports, setExports] = useState<FullchainArchiveExport[]>([])
  const [selected, setSelected] = useState<Key[]>([])
  const [stages, setStages] = useState<Array<'direct' | 'review' | 'registered'>>(['direct', 'review', 'registered'])
  const [keywordInput, setKeywordInput] = useState('')
  const keyword = useDebouncedValue(keywordInput.trim(), 350)
  const [loading, setLoading] = useState(false)
  const [selectingAll, setSelectingAll] = useState(false)
  const [error, setError] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [total, setTotal] = useState(0)
  const candidateRequestId = useRef(0)

  const loadCandidates = useCallback(async () => {
    const requestId = ++candidateRequestId.current
    setLoading(true)
    try {
      const candidateResult = await searchFullchainArchiveCandidates({ parser_type: parserType, stages, keyword, page, page_size: pageSize })
      if (requestId !== candidateRequestId.current) return
      setRows(candidateResult.data)
      setTotal(candidateResult.total)
      setError('')
    } catch (reason: unknown) {
      if (requestId === candidateRequestId.current) {
        setError(apiErrorMessage(reason, '反馈归档数据读取失败'))
      }
    } finally {
      if (requestId === candidateRequestId.current) setLoading(false)
    }
  }, [keyword, page, pageSize, parserType, stages])
  const loadExports = useCallback(async () => {
    try { setExports((await listFullchainArchiveExports(parserType)).data) }
    catch (reason: unknown) { setError(apiErrorMessage(reason, '反馈归档历史读取失败')) }
  }, [parserType])
  const load = useCallback(async () => {
    await Promise.all([loadCandidates(), loadExports()])
  }, [loadCandidates, loadExports])
  useEffect(() => { void loadCandidates() }, [loadCandidates])
  useEffect(() => { void loadExports() }, [loadExports])
  useEffect(() => {
    if (!exports.some(item => item.status === 'queued' || item.status === 'running')) return
    const timer = window.setInterval(() => void loadExports(), 2500)
    return () => window.clearInterval(timer)
  }, [exports, loadExports])

  useEffect(() => {
    setPage(1)
    setSelected([])
  }, [keyword, parserType, stages])

  const reviewOptions = useCallback((row: FullchainArchiveCandidate) => {
    if (row.result === '移交') return [{ value: 'transfer_internal', label: '确认为所内移交' }, { value: 'transfer_external', label: '确认为所外移交并可归档' }]
    if (parserType === '全链条' && row.result === '移交（所内）') return [{ value: 'keep', label: '保留在任务池' }, { value: 'archive', label: '允许反馈归档' }]
    return []
  }, [parserType])
  const saveReview = useCallback(async (row: FullchainArchiveCandidate, decision: 'transfer_internal' | 'transfer_external' | 'keep' | 'archive') => {
    try { await saveFullchainArchiveReview({ parser_type: parserType, row_key: row.row_key, decision }); message.success('审核决定已保存'); await load() }
    catch (reason: unknown) { message.error(apiErrorMessage(reason, '审核决定保存失败')) }
  }, [load, parserType])
  const confirmExport = async () => {
    const ids = selected.map(Number)
    if (!ids.length) return
    try {
      const preview = await previewFullchainArchiveExport(ids, parserType)
      Modal.confirm({
        title: `确认导出并归档 ${preview.total} 条数据？`,
        width: 680,
        content: <div className="grid gap-3">
          <Alert type="warning" showIcon message="导出即归档" description="确认后先永久保存反馈 XLSX，再由后台从腾讯在线表整行删除；已删除任务进入历史，不再出现在网格员当前任务中。" />
          <div className="flex flex-wrap gap-2">{Object.entries(preview.categories).map(([label, count]) => <Tag key={label} color="blue">{label} {count} 条</Tag>)}</div>
          <Table
            size="small"
            rowKey="source_id"
            pagination={false}
            scroll={{ x: 620, y: 260 }}
            dataSource={preview.rows}
            columns={[
              { title: '姓名', dataIndex: 'name', width: 100 },
              { title: '核查结果', dataIndex: 'result', width: 110 },
              { title: '归档类别', dataIndex: 'category', width: 120 },
              { title: '说明', dataIndex: 'reason', ellipsis: true },
            ]}
          />
          {preview.total > preview.rows.length && <span className="text-xs text-[var(--app-text-secondary)]">当前展示前 {preview.rows.length} 条，实际将处理 {preview.total} 条。</span>}
        </div>,
        okText: '确认导出并归档', cancelText: '取消',
        onOk: async () => {
          try {
            const result = await createFullchainArchiveExport(ids, preview.preview_token, parserType)
            message.success(result.message)
            setSelected([])
            await load()
          } catch (reason: unknown) {
            message.error(apiErrorMessage(reason, '归档任务创建失败'))
            throw reason
          }
        },
      })
    } catch (reason: unknown) { message.error(apiErrorMessage(reason, '归档预览失败')) }
  }

  const selectAllEligible = async () => {
    setSelectingAll(true)
    try {
      const result = await selectFullchainArchiveCandidates({ parser_type: parserType, stages, keyword })
      setSelected(result.source_ids)
      message.success(`已选择当前筛选下全部 ${result.total} 条可选数据`)
    } catch (reason: unknown) {
      message.error(apiErrorMessage(reason, '全选可选数据失败'))
    } finally {
      setSelectingAll(false)
    }
  }

  const columns: TableColumnsType<FullchainArchiveCandidate> = useMemo(() => [
    { title: '姓名', dataIndex: 'name', width: 110 },
    { title: '核查结果', dataIndex: 'result', width: 130, render: value => <Tag>{value}</Tag> },
    { title: '截止日期', dataIndex: 'deadline', width: 110 },
    { title: '说明', dataIndex: 'reason', ellipsis: true },
    { title: '归档类别', dataIndex: 'category', width: 130, render: value => value ? <Tag color="green">{value}</Tag> : <Tag color="orange">待审核</Tag> },
    { title: '审核决定', width: 210, render: (_, row) => {
      const options = reviewOptions(row)
      return row.stage === 'review' && options.length
        ? <Select className="w-full" value={row.decision || undefined} placeholder="请选择处理方式" options={options} onChange={value => void saveReview(row, value)} />
        : '-'
    } },
  ], [reviewOptions, saveReview])

  return (
    <Panel title={`${parserType}反馈导出与归档`} description="最终无法核实数据须经预览确认后导出；导出文件永久保留，腾讯整行删除在后台执行，并逐条显示进度和冲突。" padded={false}>
      <div className="grid gap-5 p-5">
        {error && <Alert type="error" showIcon message={error} />}
        <div className="grid gap-3 md:grid-cols-[minmax(220px,1fr)_minmax(260px,1fr)_auto]">
          <Input allowClear value={keywordInput} placeholder="搜索姓名、身份证、电话或地址" onChange={event => setKeywordInput(event.target.value)} />
          <Select mode="multiple" value={stages} options={[...STAGES]} onChange={setStages} maxTagCount="responsive" />
          <Button onClick={() => void load()} loading={loading}>刷新</Button>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <span className="text-sm text-[var(--app-text-secondary)]">已选择 {selected.length} 条，仅可选择满足归档条件的数据</span>
          <div className="flex flex-wrap gap-2">
            <Button loading={selectingAll} onClick={() => void selectAllEligible()}>全选所有可选数据</Button>
            <Button type="primary" danger disabled={!selected.length} onClick={() => void confirmExport()}>预览并导出归档</Button>
          </div>
        </div>
        <Table<FullchainArchiveCandidate>
          rowKey="source_id"
          size="small"
          loading={loading}
          dataSource={rows}
          columns={columns}
          scroll={{ x: 1050 }}
          rowSelection={{
            selectedRowKeys: selected,
            preserveSelectedRowKeys: true,
            onChange: setSelected,
            onSelectAll: checked => {
              if (checked) void selectAllEligible()
              else setSelected([])
            },
            getCheckboxProps: row => ({ disabled: !row.eligible }),
          }}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            showTotal: value => `共 ${value} 条`,
            onChange: (nextPage, nextPageSize) => {
              setPage(nextPageSize !== pageSize ? 1 : nextPage)
              setPageSize(nextPageSize)
            },
          }}
        />
        {exports.length > 0 && <div className="grid gap-3">
          <strong>反馈归档历史</strong>
          <Table<FullchainArchiveExport> rowKey="id" size="small" dataSource={exports} pagination={false} scroll={{ x: 900 }} expandable={{
            rowExpandable: item => item.items.some(detail => detail.status !== 'success'),
            expandedRowRender: item => <Table
              rowKey="source_id"
              size="small"
              pagination={false}
              dataSource={item.items.filter(detail => detail.status !== 'success')}
              columns={[
                { title: '来源编号', dataIndex: 'source_id', width: 120 },
                { title: '归档类别', dataIndex: 'category', width: 160 },
                { title: '处理状态', dataIndex: 'status', width: 120, render: value => value === 'conflict' ? '冲突' : value === 'error' ? '失败' : '等待处理' },
                {
                  title: '腾讯删除',
                  dataIndex: 'external_delete_state',
                  width: 130,
                  render: value => value === 'deleted'
                    ? '已确认删除'
                    : value === 'deleting' ? '结果待确认' : '尚未删除',
                },
                { title: '安全错误码', dataIndex: 'error_code' },
              ]}
            />,
          }} columns={[
            { title: '导出编号', dataIndex: 'export_no', width: 240 },
            { title: '状态', dataIndex: 'status', width: 120, render: value => <Tag color={value === 'completed' ? 'green' : value === 'partial' ? 'orange' : 'blue'}>{value === 'completed' ? '归档完成' : value === 'partial' ? '部分完成' : '后台处理中'}</Tag> },
            { title: '进度', width: 220, render: (_, item) => <Progress percent={item.total_count ? Math.round(((item.success_count + item.conflict_count + item.error_count) / item.total_count) * 100) : 0} format={() => `${item.success_count}/${item.total_count}`} status={item.status === 'partial' ? 'exception' : item.status === 'running' ? 'active' : 'normal'} /> },
            { title: '冲突/失败', width: 110, render: (_, item) => `${item.conflict_count}/${item.error_count}` },
            { title: '时间', dataIndex: 'created_at', width: 190, render: value => formatUTCTime(value, systemTimezone) },
            { title: '文件', width: 100, render: (_, item) => <Button type="link" href={fullchainArchiveDownloadUrl(item.id)}>下载</Button> },
          ]} />
        </div>}
      </div>
    </Panel>
  )
}
