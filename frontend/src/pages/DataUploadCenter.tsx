import { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Upload,
} from 'antd'
import type { TableColumnsType, UploadFile, UploadProps } from 'antd'
import { InboxOutlined, UploadOutlined } from '@ant-design/icons'
import AppTable from '../components/AppTable'
import PoliceDispatchPanel from '../components/PoliceDispatchPanel'
import { PageHeader, Panel } from '../components/ui'
import { useAuth } from '../context/AuthContext'
import {
  formatUTCTime,
  getVisitImportIssues,
  type PhotoImportBatch,
  workflowApi,
  uploadStarRating,
  uploadVisitDetail,
  type VisitImportIssue,
  type VisitImportResult,
} from '../api/client'

const { Dragger } = Upload
const MAX_FILE_BYTES = 20 * 1024 * 1024
const MAX_PHOTO_ZIP_BYTES = 200 * 1024 * 1024
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

function selectedUploadFile(file: File & { uid: string }): UploadFile {
  return {
    uid: file.uid,
    name: file.name,
    size: file.size,
    type: file.type,
    status: 'done',
    originFileObj: file,
  }
}

function validateWorkbook(
  file: File,
  showError: (message: string) => void,
): boolean {
  if (!file.name.toLowerCase().endsWith('.xlsx')) {
    showError('只支持 .xlsx 文件')
    return false
  }
  if (file.size > MAX_FILE_BYTES) {
    showError('XLSX 文件不能超过 20MB')
    return false
  }
  return true
}

const commonIssueColumns: TableColumnsType<VisitImportIssue> = [
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
    width: 320,
    render: value => <span className="text-slate-700">{value}</span>,
  },
]

const detailIssueColumns: TableColumnsType<VisitImportIssue> = [
  ...commonIssueColumns,
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

const ratingIssueColumns: TableColumnsType<VisitImportIssue> = [
  ...commonIssueColumns,
  {
    title: '社区',
    width: 130,
    render: (_, item) => item.row_preview['所属社区'] || '-',
  },
  {
    title: '星级',
    width: 130,
    render: (_, item) => item.row_preview['星级'] || '-',
  },
  {
    title: '得分',
    width: 100,
    render: (_, item) => item.row_preview['得分'] || '-',
  },
  {
    title: '采集时间',
    width: 180,
    render: (_, item) => item.row_preview['采集时间'] || '-',
  },
  {
    title: '地址',
    width: 300,
    ellipsis: { showTitle: false },
    render: (_, item) => (
      <Tooltip title={item.row_preview['地址'] || '-'}>
        <span>{item.row_preview['地址'] || '-'}</span>
      </Tooltip>
    ),
  },
]

export default function DataUploadCenter() {
  const { user, systemTimezone } = useAuth()
  const canUpload = Boolean(user?.permissions.includes('visit.import'))
  const canManagePoliceDispatch = Boolean(user?.permissions.includes('police.dispatch.manage'))
  const canManagePhotoImport = Boolean(
    (user?.permissions.includes('workflow.ticket.handle')
      && user.member?.position === '基础管控')
      || user?.role === 'super_admin'
      || user?.permissions.includes('workflow.ticket.manage'),
  )
  const canUseUploadCenter = canUpload || canManagePoliceDispatch || canManagePhotoImport
  const [detailFileList, setDetailFileList] = useState<UploadFile[]>([])
  const [detailFile, setDetailFile] = useState<File | null>(null)
  const [ratingFileList, setRatingFileList] = useState<UploadFile[]>([])
  const [ratingFile, setRatingFile] = useState<File | null>(null)
  const [activeImport, setActiveImport] = useState<'detail' | 'rating' | null>(null)
  const [detailError, setDetailError] = useState('')
  const [ratingError, setRatingError] = useState('')
  const [result, setResult] = useState<VisitImportResult | null>(null)
  const [issues, setIssues] = useState<VisitImportIssue[]>([])
  const [issueTotal, setIssueTotal] = useState(0)
  const [issuePage, setIssuePage] = useState(1)
  const [issueLoading, setIssueLoading] = useState(false)
  const [photoFileList, setPhotoFileList] = useState<UploadFile[]>([])
  const [photoFile, setPhotoFile] = useState<File | null>(null)
  const [photoBatch, setPhotoBatch] = useState<PhotoImportBatch | null>(null)
  const [photoError, setPhotoError] = useState('')
  const [photoLoading, setPhotoLoading] = useState(false)
  const [photoHistory, setPhotoHistory] = useState<PhotoImportBatch[]>([])

  const loadPhotoHistory = async () => {
    if (!canManagePhotoImport) return
    try {
      setPhotoHistory((await workflowApi.photoImports(1, 20)).data)
    } catch {
      // 当前批次仍可继续使用；历史列表读取失败不阻断上传。
    }
  }

  useEffect(() => { void loadPhotoHistory() }, [canManagePhotoImport])

  const resetResult = () => {
    setResult(null)
    setIssues([])
    setIssueTotal(0)
    setIssuePage(1)
  }

  const beforeDetailUpload: UploadProps['beforeUpload'] = file => {
    setDetailError('')
    setDetailFile(null)
    setDetailFileList([])
    resetResult()
    if (!validateWorkbook(file, setDetailError)) return Upload.LIST_IGNORE
    setDetailFile(file)
    setDetailFileList([selectedUploadFile(file)])
    return false
  }

  const beforeRatingUpload: UploadProps['beforeUpload'] = file => {
    setRatingError('')
    setRatingFile(null)
    setRatingFileList([])
    resetResult()
    if (!validateWorkbook(file, setRatingError)) return Upload.LIST_IGNORE
    setRatingFile(file)
    setRatingFileList([selectedUploadFile(file)])
    return false
  }

  const applyResult = (nextResult: VisitImportResult) => {
    setResult(nextResult)
    setIssues(nextResult.issues.data)
    setIssueTotal(nextResult.issues.total)
    setIssuePage(1)
  }

  const handleDetailImport = async () => {
    if (!detailFile) return
    setActiveImport('detail')
    setDetailError('')
    try {
      applyResult(await uploadVisitDetail(detailFile))
    } catch (error: any) {
      setDetailError(
        error?.response?.data?.detail
          || (error?.code === 'ECONNABORTED'
            ? '导入处理超时，请稍后查看走访汇总的数据范围'
            : '上传失败，请稍后重试'),
      )
    } finally {
      setActiveImport(null)
    }
  }

  const handleRatingImport = async () => {
    if (!ratingFile) return
    setActiveImport('rating')
    setRatingError('')
    try {
      applyResult(await uploadStarRating(ratingFile))
    } catch (error: any) {
      setRatingError(
        error?.response?.data?.detail
          || (error?.code === 'ECONNABORTED'
            ? '导入处理超时，请稍后查看走访汇总的数据范围'
            : '上传失败，请稍后重试'),
      )
    } finally {
      setActiveImport(null)
    }
  }

  const beforePhotoUpload: UploadProps['beforeUpload'] = file => {
    setPhotoError('')
    setPhotoBatch(null)
    setPhotoFile(null)
    setPhotoFileList([])
    if (!file.name.toLowerCase().endsWith('.zip')) {
      setPhotoError('照片批次只支持 ZIP 文件')
      return Upload.LIST_IGNORE
    }
    if (file.size > MAX_PHOTO_ZIP_BYTES) {
      setPhotoError('照片 ZIP 不能超过 200MB')
      return Upload.LIST_IGNORE
    }
    setPhotoFile(file)
    setPhotoFileList([selectedUploadFile(file)])
    return false
  }

  const handlePhotoPreview = async () => {
    if (!photoFile) return
    setPhotoLoading(true)
    setPhotoError('')
    try {
      setPhotoBatch(await workflowApi.previewPhotoImport(photoFile))
      await loadPhotoHistory()
    } catch (error: any) {
      setPhotoError(error?.response?.data?.detail || '照片 ZIP 解析失败，请检查文件名和内容')
    } finally {
      setPhotoLoading(false)
    }
  }

  const handlePhotoConfirm = async () => {
    if (!photoBatch) return
    setPhotoLoading(true)
    setPhotoError('')
    try {
      setPhotoBatch(await workflowApi.confirmPhotoImport(photoBatch.id))
      await loadPhotoHistory()
    } catch (error: any) {
      setPhotoError(error?.response?.data?.detail || '照片批次确认失败，请刷新后重试')
    } finally {
      setPhotoLoading(false)
    }
  }

  const handlePhotoHistoryDetail = async (batchId: number) => {
    setPhotoLoading(true)
    setPhotoError('')
    try {
      setPhotoBatch(await workflowApi.photoImport(batchId))
    } catch (error: any) {
      setPhotoError(error?.response?.data?.detail || '照片批次详情读取失败，请稍后重试')
    } finally {
      setPhotoLoading(false)
    }
  }

  const loadIssuePage = async (page: number) => {
    if (!result || (result.status === 'duplicate' && issueTotal === 0)) return
    setIssueLoading(true)
    try {
      const response = await getVisitImportIssues(
        result.batch_id,
        page,
        ISSUE_PAGE_SIZE,
      )
      setIssues(response.data)
      setIssueTotal(response.total)
      setIssuePage(page)
    } catch {
      if (result.import_type === 'rating') {
        setRatingError('导入问题明细读取失败，请稍后重试')
      } else {
        setDetailError('导入问题明细读取失败，请稍后重试')
      }
    } finally {
      setIssueLoading(false)
    }
  }

  return (
    <div className="app-page min-w-0">
      <PageHeader
        title="数据上传中心"
        description="集中导入走访、星级评定和全链条下发文件，并查看各类处理结果"
      />

      {!canUseUploadCenter && (
        <Alert
          type="info"
          showIcon
          message="当前账号没有上传权限"
          description="当前账号没有可用的数据上传权限。"
        />
      )}

      <div className="grid min-w-0 gap-4 xl:grid-cols-2">
        <Panel
          title="上传走访明细"
          description="先导入走访明细；重叠日期会自动去重"
        >
          <Dragger
            accept=".xlsx"
            maxCount={1}
            fileList={detailFileList}
            beforeUpload={beforeDetailUpload}
            onRemove={() => {
              setDetailFile(null)
              setDetailFileList([])
              resetResult()
            }}
            disabled={!canUpload || activeImport !== null}
          >
            <p className="ant-upload-drag-icon"><InboxOutlined /></p>
            <p className="ant-upload-text">拖入走访明细，或点击选择 XLSX</p>
            <p className="ant-upload-hint">
              同一网格员同日同地址取时间最晚的一条，不同网格员分别保留。
            </p>
          </Dragger>
          <div className="mt-4 flex justify-end">
            <Button
              type="primary"
              icon={<UploadOutlined />}
              loading={activeImport === 'detail'}
              disabled={!canUpload || !detailFile || activeImport !== null}
              onClick={handleDetailImport}
            >
              {activeImport === 'detail' ? '正在校验并入库' : '导入走访明细'}
            </Button>
          </div>
          {detailError && (
            <Alert className="mt-4" type="error" showIcon message={detailError} />
          )}
        </Panel>

        <Panel
          title="上传星级评定"
          description="按地址匹配采集时间前后 24 小时内最接近的走访"
        >
          <Dragger
            accept=".xlsx"
            maxCount={1}
            fileList={ratingFileList}
            beforeUpload={beforeRatingUpload}
            onRemove={() => {
              setRatingFile(null)
              setRatingFileList([])
              resetResult()
            }}
            disabled={!canUpload || activeImport !== null}
          >
            <p className="ant-upload-drag-icon"><InboxOutlined /></p>
            <p className="ant-upload-text">拖入星级评定，或点击选择 XLSX</p>
            <p className="ant-upload-hint">
              星级评定必须匹配已有走访；无法判断时不会强行关联。
            </p>
          </Dragger>
          <div className="mt-4 flex justify-end">
            <Button
              type="primary"
              icon={<UploadOutlined />}
              loading={activeImport === 'rating'}
              disabled={!canUpload || !ratingFile || activeImport !== null}
              onClick={handleRatingImport}
            >
              {activeImport === 'rating' ? '正在匹配并关联' : '导入星级评定'}
            </Button>
          </div>
          {ratingError && (
            <Alert className="mt-4" type="error" showIcon message={ratingError} />
          )}
        </Panel>
      </div>

      {canManagePhotoImport && (
        <Panel
          title="照片调取批次"
          description="文件名使用“姓名_身份证号.jpg”格式；先预览匹配结果，确认后才会挂载照片并完成工单。"
        >
          <Dragger
            accept=".zip"
            maxCount={1}
            fileList={photoFileList}
            beforeUpload={beforePhotoUpload}
            onRemove={() => {
              setPhotoFile(null)
              setPhotoFileList([])
              setPhotoBatch(null)
              setPhotoError('')
            }}
            disabled={photoLoading}
          >
            <p className="ant-upload-drag-icon"><InboxOutlined /></p>
            <p className="ant-upload-text">拖入照片 ZIP，或点击选择</p>
            <p className="ant-upload-hint">支持 JPG、PNG、WebP、HEIC；单个 ZIP 最大 200MB。</p>
          </Dragger>
          <div className="mt-3 flex justify-end gap-2">
            <Button
              type="primary"
              icon={<UploadOutlined />}
              loading={photoLoading && !photoBatch}
              disabled={!photoFile || photoLoading || Boolean(photoBatch && photoBatch.status !== 'preview')}
              onClick={() => void handlePhotoPreview()}
            >预览匹配结果</Button>
            {photoBatch?.status === 'preview' && (
              <Button
                type="primary"
                loading={photoLoading}
                disabled={photoLoading}
                onClick={() => void handlePhotoConfirm()}
              >确认导入并通知</Button>
            )}
          </div>
          {photoError && <Alert className="mt-3" type="error" showIcon message={photoError} />}
          {photoBatch && (
            <div className="mt-4 space-y-3">
              <Alert
                type={photoBatch.status === 'completed' ? 'success' : photoBatch.status === 'partial' ? 'warning' : 'info'}
                showIcon
                message={`批次 ${photoBatch.batch_no}：${photoBatch.status === 'preview' ? '等待确认' : photoBatch.status === 'completed' ? '已完成' : photoBatch.status === 'partial' ? '部分完成' : '处理中'}`}
              />
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
                {[
                  ['照片总数', photoBatch.total_files],
                  ['可匹配', photoBatch.matched_files],
                  ['未匹配', photoBatch.unmatched_files],
                  ['冲突提醒', photoBatch.conflict_files],
                  ['重复', photoBatch.duplicate_files],
                  ['失败', photoBatch.failed_files],
                ].map(([label, value]) => (
                  <div key={String(label)} className="rounded-lg border border-[var(--app-border)] p-3">
                    <Statistic title={label} value={value} suffix="张" />
                  </div>
                ))}
              </div>
              {(photoBatch.items?.length || 0) > 0 && (
                <Table
                  size="small"
                  rowKey={(row: any) => `${row.safe_name}-${row.sha256}`}
                  dataSource={photoBatch.items}
                  pagination={{ pageSize: 10, showSizeChanger: false }}
                  scroll={{ x: 760 }}
                  columns={[
                    { title: '文件名', dataIndex: 'safe_name', width: 260, ellipsis: true },
                    { title: '姓名', dataIndex: 'person_name', width: 100 },
                    { title: '身份证号', dataIndex: 'identity_number', width: 190 },
                    {
                      title: '匹配结果', dataIndex: 'match_status', width: 110,
                      render: value => <Tag color={value === 'matched' ? 'green' : value === 'duplicate' ? 'blue' : value === 'unmatched' ? 'orange' : value === 'conflict' ? 'gold' : 'red'}>{value === 'matched' ? '可匹配' : value === 'duplicate' ? '重复' : value === 'unmatched' ? '未匹配' : value === 'conflict' ? '冲突提醒' : '失败'}</Tag>,
                    },
                    { title: '说明', dataIndex: 'match_reason', width: 260, ellipsis: true },
                  ]}
                />
              )}
            </div>
          )}
          {photoHistory.length > 0 && (
            <div className="mt-5">
              <div className="mb-2 text-sm font-medium text-[var(--app-text-strong)]">最近照片批次</div>
              <Table
                size="small"
                rowKey="id"
                dataSource={photoHistory}
                pagination={false}
                scroll={{ x: 760 }}
                columns={[
                  { title: '批次', dataIndex: 'batch_no', width: 230 },
                  { title: '状态', dataIndex: 'status', width: 100, render: value => value === 'completed' ? '已完成' : value === 'partial' ? '部分完成' : value === 'preview' ? '待确认' : '处理中' },
                  { title: '照片', dataIndex: 'total_files', width: 80 },
                  { title: '可匹配', dataIndex: 'matched_files', width: 90 },
                  { title: '未匹配', dataIndex: 'unmatched_files', width: 90 },
                  { title: '时间', dataIndex: 'created_at', width: 190, render: value => formatUTCTime(value, systemTimezone) },
                  {
                    title: '操作', width: 90, fixed: 'right',
                    render: (_: unknown, row: PhotoImportBatch) => (
                      <Button type="link" size="small" onClick={() => void handlePhotoHistoryDetail(row.id)}>
                        查看
                      </Button>
                    ),
                  },
                ]}
              />
            </div>
          )}
        </Panel>
      )}

      <PoliceDispatchPanel enabled={canManagePoliceDispatch} />

      {result && (
        <Panel
          title="本次导入结果"
          extra={(
            <Tag color={statusMeta[result.status].color}>
              {statusMeta[result.status].label}
            </Tag>
          )}
        >
          <Alert
            className="mb-4"
            type={result.status === 'failed'
              ? 'error'
              : result.status === 'partial' ? 'warning' : 'success'}
            showIcon
            message={result.message}
            description={(
              <span>
                文件范围：
                <DateRange start={result.file_start_date} end={result.file_end_date} />
                {result.overlap_start_date && (
                  <>
                    ；与旧数据重叠：
                    <DateRange
                      start={result.overlap_start_date}
                      end={result.overlap_end_date}
                    />
                  </>
                )}
                ；导入后数据库：
                <DateRange
                  start={result.coverage.start_date}
                  end={result.coverage.end_date}
                />
              </span>
            )}
          />
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-7">
            {(result.import_type === 'rating'
              ? [
                  ['新增评定', result.inserted_rows],
                  ['更新评定', result.updated_rows],
                  ['重复未变', result.unchanged_rows],
                  ['无法匹配', result.unmatched_rows || 0],
                  ['匹配有歧义', result.ambiguous_rows || 0],
                  ['未采用', result.ignored_rows],
                  ['错误/提醒', result.error_count + result.warning_count],
                ]
              : [
                  ['新增', result.inserted_rows],
                  ['更新', result.updated_rows],
                  ['重复未变', result.unchanged_rows],
                  ['忽略', result.ignored_rows],
                  ['错误', result.error_count],
                  ['提醒', result.warning_count],
                ]).map(([label, value]) => (
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
          description={result.import_type === 'rating'
            ? '无法匹配或存在歧义的星级评定不会强行写入走访记录'
            : '身份证号已经遮盖；错误行未入库，提醒行不影响有效数据入库'}
          padded={false}
        >
          <AppTable<VisitImportIssue>
            columns={result.import_type === 'rating'
              ? ratingIssueColumns
              : detailIssueColumns}
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
    </div>
  )
}
