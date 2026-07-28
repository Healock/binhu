import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Modal,
  Statistic,
  Tag,
  Tooltip,
  Upload,
} from 'antd'
import type { TableColumnsType, UploadFile, UploadProps } from 'antd'
import {
  CalendarOutlined,
  InboxOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import AppTable from '../components/AppTable'
import { PageHeader, Panel } from '../components/ui'
import { useAuth } from '../context/AuthContext'
import {
  formatUTCTime,
  getVisitCoverage,
  getVisitImportIssues,
  uploadVisitDetail,
  type VisitCoverage,
  type VisitImportIssue,
  type VisitImportResult,
} from '../api/client'

const { Dragger } = Upload
const MAX_FILE_BYTES = 20 * 1024 * 1024
const ISSUE_PAGE_SIZE = 50

const statusMeta = {
  success: { color: 'success', label: '导入成功' },
  partial: { color: 'warning', label: '部分成功' },
  failed: { color: 'error', label: '导入失败' },
  duplicate: { color: 'default', label: '文件已导入' },
} as const

function DateRange({ start, end }: { start: string | null; end: string | null }) {
  if (!start || !end) return <span className="text-slate-400">暂无数据</span>
  return <span>{start} 至 {end}</span>
}

export default function VisitSummary() {
  const { user } = useAuth()
  const canUpload = user?.role === 'super_admin' || user?.role === 'admin'
  const [coverage, setCoverage] = useState<VisitCoverage | null>(null)
  const [coverageLoading, setCoverageLoading] = useState(false)
  const [coverageError, setCoverageError] = useState('')
  const [missingOpen, setMissingOpen] = useState(false)
  const [fileList, setFileList] = useState<UploadFile[]>([])
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [importing, setImporting] = useState(false)
  const [importError, setImportError] = useState('')
  const [result, setResult] = useState<VisitImportResult | null>(null)
  const [issues, setIssues] = useState<VisitImportIssue[]>([])
  const [issueTotal, setIssueTotal] = useState(0)
  const [issuePage, setIssuePage] = useState(1)
  const [issueLoading, setIssueLoading] = useState(false)

  const loadCoverage = useCallback(async () => {
    setCoverageLoading(true)
    setCoverageError('')
    try {
      setCoverage(await getVisitCoverage())
    } catch {
      setCoverageError('走访数据范围读取失败，请稍后重试')
    } finally {
      setCoverageLoading(false)
    }
  }, [])

  useEffect(() => {
    loadCoverage()
  }, [loadCoverage])

  const beforeUpload: UploadProps['beforeUpload'] = file => {
    setImportError('')
    setResult(null)
    setSelectedFile(null)
    setFileList([])
    setIssues([])
    setIssueTotal(0)
    if (!file.name.toLowerCase().endsWith('.xlsx')) {
      setImportError('只支持 .xlsx 文件')
      return Upload.LIST_IGNORE
    }
    if (file.size > MAX_FILE_BYTES) {
      setImportError('XLSX 文件不能超过 20MB')
      return Upload.LIST_IGNORE
    }
    setSelectedFile(file)
    setFileList([{
      uid: file.uid,
      name: file.name,
      size: file.size,
      type: file.type,
      status: 'done',
      originFileObj: file,
    }])
    return false
  }

  const handleImport = async () => {
    if (!selectedFile) return
    setImporting(true)
    setImportError('')
    try {
      const nextResult = await uploadVisitDetail(selectedFile)
      setResult(nextResult)
      setCoverage(nextResult.coverage)
      setIssues(nextResult.issues.data)
      setIssueTotal(nextResult.issues.total)
      setIssuePage(1)
    } catch (error: any) {
      setImportError(
        error?.response?.data?.detail
          || (error?.code === 'ECONNABORTED' ? '导入处理超时，请稍后确认数据范围' : '上传失败，请稍后重试'),
      )
    } finally {
      setImporting(false)
    }
  }

  const loadIssuePage = async (page: number) => {
    if (!result || (result.status === 'duplicate' && issueTotal === 0)) return
    setIssueLoading(true)
    try {
      const response = await getVisitImportIssues(result.batch_id, page, ISSUE_PAGE_SIZE)
      setIssues(response.data)
      setIssueTotal(response.total)
      setIssuePage(page)
    } catch {
      setImportError('导入问题明细读取失败，请稍后重试')
    } finally {
      setIssueLoading(false)
    }
  }

  const issueColumns: TableColumnsType<VisitImportIssue> = [
    {
      title: '类型',
      dataIndex: 'severity',
      width: 90,
      render: value => (
        <Tag color={value === 'error' ? 'error' : 'warning'}>
          {value === 'error' ? '错误' : '提醒'}
        </Tag>
      ),
    },
    {
      title: 'Excel 行',
      dataIndex: 'row_number',
      width: 100,
      render: value => value || '-',
    },
    {
      title: '原因',
      dataIndex: 'message',
      width: 300,
      render: value => <span className="text-slate-700">{value}</span>,
    },
    {
      title: '社区',
      render: (_, item) => item.row_preview['村社区'] || '-',
      width: 130,
    },
    {
      title: '操作人',
      render: (_, item) => item.row_preview['操作人'] || '-',
      width: 110,
    },
    {
      title: '入户时间',
      render: (_, item) => item.row_preview['入户时间'] || '-',
      width: 170,
    },
    {
      title: '地址',
      width: 280,
      ellipsis: { showTitle: false },
      render: (_, item) => (
        <Tooltip title={item.row_preview['地址'] || '-'}>
          <span>{item.row_preview['地址'] || '-'}</span>
        </Tooltip>
      ),
    },
    {
      title: '操作人账号',
      render: (_, item) => item.row_preview['操作人账号'] || '-',
      width: 180,
    },
  ]

  const shownMissingDates = coverage?.missing_dates.slice(0, 10) || []

  return (
    <div className="app-page min-w-0">
      <PageHeader
        title="走访汇总"
        description="第一阶段先导入走访明细，星级评定与最终汇总将在下一阶段接入"
      />

      <Panel
        title="当前数据库数据范围"
        description="上传前先确认现有日期范围，重叠日期会自动合并去重"
        extra={
          <Button
            icon={<CalendarOutlined />}
            loading={coverageLoading}
            onClick={loadCoverage}
          >
            刷新范围
          </Button>
        }
      >
        {coverageError && <Alert className="mb-4" type="error" showIcon message={coverageError} />}
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <div className="rounded-lg border border-slate-200 bg-slate-50 p-4 xl:col-span-2">
            <div className="text-xs text-slate-500">已入库日期范围</div>
            <div className="mt-2 text-lg font-semibold text-slate-900">
              <DateRange start={coverage?.start_date || null} end={coverage?.end_date || null} />
            </div>
          </div>
          <div className="rounded-lg border border-slate-200 p-4">
            <Statistic title="有效走访记录" value={coverage?.total_records || 0} suffix="条" />
          </div>
          <div className="rounded-lg border border-slate-200 p-4">
            <Statistic title="有数据日期" value={coverage?.data_days || 0} suffix="天" />
          </div>
          <div className="rounded-lg border border-slate-200 p-4">
            <Statistic title="无数据日期" value={coverage?.missing_date_count || 0} suffix="天" />
          </div>
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-2 text-sm text-slate-500">
          <span>最近成功导入：{formatUTCTime(coverage?.last_import_at)}</span>
          {shownMissingDates.map(item => <Tag key={item}>{item}</Tag>)}
          {(coverage?.missing_date_count || 0) > 10 && (
            <Button type="link" size="small" onClick={() => setMissingOpen(true)}>
              查看全部
            </Button>
          )}
        </div>
      </Panel>

      <Panel
        title="上传走访明细"
        description="仅支持一个 XLSX 文件，最大 20MB；正确行会入库，错误行会单独列出"
      >
        {!canUpload ? (
          <Alert
            type="info"
            showIcon
            message="只有超级管理员和管理员可以上传走访数据"
            description="你仍然可以查看上方数据库日期范围。"
          />
        ) : (
          <>
            <Dragger
              accept=".xlsx"
              maxCount={1}
              fileList={fileList}
              beforeUpload={beforeUpload}
              onRemove={() => {
                setSelectedFile(null)
                setFileList([])
                setResult(null)
                setIssues([])
                setIssueTotal(0)
              }}
              disabled={importing}
            >
              <p className="ant-upload-drag-icon"><InboxOutlined /></p>
              <p className="ant-upload-text">拖入走访明细，或点击选择 XLSX 文件</p>
              <p className="ant-upload-hint">重叠日期不会重复计数，同日同地址取入户时间最晚的一条。</p>
            </Dragger>
            <div className="mt-4 flex justify-end">
              <Button
                type="primary"
                icon={<UploadOutlined />}
                loading={importing}
                disabled={!selectedFile}
                onClick={handleImport}
              >
                {importing ? '正在校验并入库' : '开始导入'}
              </Button>
            </div>
          </>
        )}
        {importError && <Alert className="mt-4" type="error" showIcon message={importError} />}
      </Panel>

      {result && (
        <Panel
          title="本次导入结果"
          extra={<Tag color={statusMeta[result.status].color}>{statusMeta[result.status].label}</Tag>}
        >
          <Alert
            className="mb-4"
            type={result.status === 'failed' ? 'error' : result.status === 'partial' ? 'warning' : 'success'}
            showIcon
            message={result.message}
            description={
              <span>
                文件范围：<DateRange start={result.file_start_date} end={result.file_end_date} />
                {result.overlap_start_date && (
                  <>；与旧数据重叠：<DateRange start={result.overlap_start_date} end={result.overlap_end_date} /></>
                )}
                ；导入后数据库：<DateRange start={result.coverage.start_date} end={result.coverage.end_date} />
              </span>
            }
          />
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
            {[
              ['新增', result.inserted_rows],
              ['更新', result.updated_rows],
              ['重复未变', result.unchanged_rows],
              ['忽略', result.ignored_rows],
              ['错误', result.error_count],
              ['提醒', result.warning_count],
            ].map(([label, value]) => (
              <div key={String(label)} className="rounded-lg border border-slate-200 p-4">
                <Statistic title={label} value={value} suffix="条" />
              </div>
            ))}
          </div>
        </Panel>
      )}

      {result && issueTotal > 0 && (
        <Panel
          title={`错误和提醒（${issueTotal} 条）`}
          description="身份证号已经遮盖；错误行未入库，提醒行不影响有效数据入库"
          padded={false}
        >
          <AppTable<VisitImportIssue>
            columns={issueColumns}
            dataSource={issues}
            rowKey="id"
            loading={issueLoading}
            pagination={{
              current: issuePage,
              pageSize: ISSUE_PAGE_SIZE,
              total: issueTotal,
              showSizeChanger: false,
              showTotal: total => `共 ${total} 条`,
              onChange: loadIssuePage,
            }}
            scroll={{ x: 1360 }}
          />
        </Panel>
      )}

      <Modal
        open={missingOpen}
        title={`无数据日期（${coverage?.missing_date_count || 0} 天）`}
        footer={null}
        onCancel={() => setMissingOpen(false)}
      >
        <div className="flex max-h-[55vh] flex-wrap gap-2 overflow-y-auto py-2">
          {coverage?.missing_dates.map(item => <Tag key={item}>{item}</Tag>)}
        </div>
      </Modal>
    </div>
  )
}
