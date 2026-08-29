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
import FullchainPoliceRawPanel from './FullchainPoliceRawPanel'
import { Panel } from './ui'

const STAGES = [
  { value: 'direct', label: '可直接反馈' },
  { value: 'review', label: '待基础管控审核' },
  { value: 'registered', label: '已登记确认' },
] as const

const ERROR_STAGE_LABELS: Record<string, string> = {
  external_delete: '历史外部移除步骤',
  registration_evidence: '登记确认复核',
  source_snapshot: '冻结快照',
  archive_schema: '归档结构',
  archive_insert: '历史写入',
  current_row_remove: '当前数据移除',
  review_flow_archive: '研判流程归档',
  transaction_begin: '事务启动',
  transaction_commit: '事务提交',
  transaction_rollback: '事务回滚',
  cache_refresh: '本地投影刷新',
  reconcile_source: '平台来源对账',
  reconcile_schema: '对账结构检查',
  reconcile_archive_compare: '历史内容对账',
  reconcile_current_compare: '当前内容对账',
  reconcile_current_remove: '当前数据清理',
  reconcile_archive_insert: '平台补写历史',
  reconcile_platform: '平台补偿',
}

const ERROR_CODE_LABELS: Record<string, string> = {
  source_row_changed: '归档前来源已变化，未移除当前任务',
  registration_archive_evidence_changed: '居住证确认、房屋或来源状态已变化，当前任务未归档',
  source_snapshot_missing: '冻结快照缺失或不完整，无法安全补偿',
  archive_schema_mismatch: '归档表结构与当前业务不兼容',
  archive_insert_failed: '历史归档写入失败',
  current_row_remove_failed: '当前业务数据移除失败',
  review_flow_state_conflict: '研判流程仍在处理中，禁止归档',
  review_flow_archive_failed: '研判流程归档失败',
  archive_transaction_deadlock: '数据库事务发生死锁，可安全重试平台步骤',
  archive_transaction_timeout: '数据库事务等待超时，可安全重试平台步骤',
  archive_database_unavailable: '数据库暂时不可用',
  external_delete_rejected: '历史外部移除步骤被拒绝，未继续平台归档',
  external_delete_outcome_unknown: '历史外部移除结果不确定，禁止自动重试',
  cache_refresh_pending: '平台已归档，等待本地投影刷新',
  archive_content_conflict: '历史数据与导出冻结快照不一致，需人工核对',
  current_row_changed_after_external_delete: '历史外部移除后平台当前数据已变化，禁止覆盖',
}

const PLATFORM_STATE_LABELS: Record<string, string> = {
  pending: '等待平台归档',
  archiving: '平台归档中',
  archived: '平台已归档',
  failed: '平台归档未完成',
  reconciled: '平台对账已完成',
}

const RECONCILE_STATE_LABELS: Record<string, string> = {
  pending: '尚未对账',
  reconciling: '正在仅平台对账',
  reconciled_by_sync: '已由历史同步归档并完成对账',
  reconciled_from_current: '已从当前数据补偿归档',
  reconciled_from_snapshot: '已从冻结快照补偿归档',
  conflict: '平台对账冲突',
}

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
          <Alert type="warning" showIcon message="导出即归档" description="确认后先永久保存反馈 XLSX，再由后台通过本地事务写入历史归档并移出当前任务池。" />
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
    {
      title: '已登记确认时间',
      dataIndex: 'registration_confirmed_at',
      width: 190,
      render: value => value ? formatUTCTime(value, systemTimezone) : '-',
    },
    {
      title: '归档保留期至',
      dataIndex: 'archive_available_at',
      width: 190,
      render: value => value ? formatUTCTime(value, systemTimezone) : '-',
    },
    { title: '归档类别', dataIndex: 'category', width: 130, render: value => value ? <Tag color="green">{value}</Tag> : <Tag color="orange">待审核</Tag> },
    { title: '审核决定', width: 210, render: (_, row) => {
      const options = reviewOptions(row)
      return row.stage === 'review' && options.length
        ? <Select className="w-full" value={row.decision || undefined} placeholder="请选择处理方式" options={options} onChange={value => void saveReview(row, value)} />
        : '-'
    } },
  ], [reviewOptions, saveReview, systemTimezone])

  return (
    <Panel title={`${parserType}反馈导出与归档`} description="最终无法核实数据须经预览确认后导出；导出文件永久保留，本地归档在后台执行，并逐条显示进度和冲突。" padded={false}>
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
          scroll={{ x: 1430 }}
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
            rowExpandable: item => item.items.some(detail => detail.status !== 'success' || detail.reconcile_state.startsWith('reconciled_')),
            expandedRowRender: item => <Table
              rowKey="source_id"
              size="small"
              pagination={false}
              dataSource={item.items.filter(detail => detail.status !== 'success' || detail.reconcile_state.startsWith('reconciled_'))}
              scroll={{ x: 1080 }}
              columns={[
                { title: '来源编号', dataIndex: 'source_id', width: 120 },
                { title: '归档类别', dataIndex: 'category', width: 130 },
                {
                  title: '处理状态',
                  dataIndex: 'status',
                  width: 120,
                  render: value => value === 'success'
                    ? '已完成'
                    : value === 'conflict'
                      ? '冲突'
                      : value === 'error'
                        ? '失败'
                        : '等待处理',
                },
                {
                  title: '移出当前任务池',
                  dataIndex: 'external_delete_state',
                  width: 130,
                  render: value => value === 'deleted'
                    ? '已移出'
                    : value === 'deleting' ? '处理中' : '等待处理',
                },
                {
                  title: '平台归档',
                  dataIndex: 'platform_archive_state',
                  width: 150,
                  render: value => PLATFORM_STATE_LABELS[value] || value,
                },
                {
                  title: '平台对账',
                  dataIndex: 'reconcile_state',
                  width: 210,
                  render: value => RECONCILE_STATE_LABELS[value] || value,
                },
                {
                  title: '失败阶段',
                  dataIndex: 'error_stage',
                  width: 150,
                  render: value => value ? (ERROR_STAGE_LABELS[value] || value) : '-',
                },
                {
                  title: '处理说明',
                  dataIndex: 'error_code',
                  width: 260,
                  render: value => value ? (ERROR_CODE_LABELS[value] || value) : '-',
                },
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
        {parserType === '全链条' && <FullchainPoliceRawPanel enabled />}
      </div>
    </Panel>
  )
}
