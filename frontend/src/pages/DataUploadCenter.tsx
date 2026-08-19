import { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Modal,
  Statistic,
  Table,
  Tag,
  Upload,
} from 'antd'
import type { UploadFile, UploadProps } from 'antd'
import { InboxOutlined, UploadOutlined } from '@ant-design/icons'
import PoliceDispatchPanel from '../components/PoliceDispatchPanel'
import { PageHeader, Panel } from '../components/ui'
import { useAuth } from '../context/AuthContext'
import {
  formatUTCTime,
  type PhotoImportBatch,
  type PhotoImportReconcileResult,
  workflowApi,
} from '../api/client'

const { Dragger } = Upload
const MAX_PHOTO_ZIP_BYTES = 200 * 1024 * 1024

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


export default function DataUploadCenter() {
  const { user, systemTimezone } = useAuth()
  const canManagePoliceDispatch = Boolean(user?.permissions.includes('police.dispatch.manage'))
  const canManagePhotoImport = Boolean(
    (user?.permissions.includes('workflow.ticket.handle')
      && user.member?.position === '基础管控')
      || user?.role === 'super_admin'
      || user?.permissions.includes('workflow.ticket.manage'),
  )
  const canUseUploadCenter = canManagePoliceDispatch || canManagePhotoImport
  const [photoFileList, setPhotoFileList] = useState<UploadFile[]>([])
  const [photoFile, setPhotoFile] = useState<File | null>(null)
  const [photoBatch, setPhotoBatch] = useState<PhotoImportBatch | null>(null)
  const [photoError, setPhotoError] = useState('')
  const [photoLoading, setPhotoLoading] = useState(false)
  const [photoHistory, setPhotoHistory] = useState<PhotoImportBatch[]>([])
  const [photoReconcile, setPhotoReconcile] = useState<PhotoImportReconcileResult | null>(null)

  const loadPhotoHistory = async () => {
    if (!canManagePhotoImport) return
    try {
      setPhotoHistory((await workflowApi.photoImports(1, 20)).data)
    } catch {
      // 当前批次仍可继续使用；历史列表读取失败不阻断上传。
    }
  }

  useEffect(() => { void loadPhotoHistory() }, [canManagePhotoImport])
  const beforePhotoUpload: UploadProps['beforeUpload'] = file => {
    setPhotoError('')
    setPhotoBatch(null)
    setPhotoReconcile(null)
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
      setPhotoReconcile(null)
      await loadPhotoHistory()
    } catch (error: any) {
      setPhotoError(
        error?.response?.data?.detail
          || (error?.response?.status === 413
            ? '照片 ZIP 超过服务器上传限制，请联系管理员检查网关配置'
            : error?.code === 'ECONNABORTED'
              ? '照片 ZIP 上传或解析超时，请稍后重试'
              : '照片 ZIP 解析失败，请检查文件名和内容'),
      )
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
      setPhotoReconcile(null)
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
    setPhotoReconcile(null)
    try {
      setPhotoBatch(await workflowApi.photoImport(batchId))
    } catch (error: any) {
      setPhotoError(error?.response?.data?.detail || '照片批次详情读取失败，请稍后重试')
    } finally {
      setPhotoLoading(false)
    }
  }

  const handlePhotoReconcilePreview = async () => {
    if (!photoBatch) return
    setPhotoLoading(true)
    setPhotoError('')
    try {
      setPhotoReconcile(await workflowApi.previewPhotoImportReconcile(photoBatch.id))
    } catch (error: any) {
      setPhotoError(error?.response?.data?.detail || '遗漏工单核对失败，请稍后重试')
    } finally {
      setPhotoLoading(false)
    }
  }

  const handlePhotoReconcileConfirm = () => {
    if (!photoBatch || !photoReconcile?.eligible_tickets) return
    Modal.confirm({
      title: '确认修复遗漏照片工单？',
      content: `将补齐并完成 ${photoReconcile.eligible_tickets} 张工单，其中复制 ${photoReconcile.attachment_copies} 份照片附件。`,
      okText: '确认修复',
      cancelText: '取消',
      onOk: async () => {
        setPhotoLoading(true)
        setPhotoError('')
        try {
          const result = await workflowApi.reconcilePhotoImport(photoBatch.id)
          setPhotoReconcile(result)
          await loadPhotoHistory()
        } catch (error: any) {
          setPhotoError(error?.response?.data?.detail || '遗漏工单修复失败，未修改批次数据')
        } finally {
          setPhotoLoading(false)
        }
      },
    })
  }

  return (
    <div className="app-page min-w-0">
      <PageHeader
        title="数据上传中心"
        description="集中处理全链条下发文件和照片批次，并查看处理进度"
      />

      {!canUseUploadCenter && (
        <Alert
          type="info"
          showIcon
          message="当前账号没有上传权限"
          description="当前账号没有可用的数据上传权限。"
        />
      )}

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
            {photoBatch && ['completed', 'partial'].includes(photoBatch.status) && (
              <Button
                loading={photoLoading}
                disabled={photoLoading}
                onClick={() => void handlePhotoReconcilePreview()}
              >核对遗漏工单</Button>
            )}
          </div>
          {photoError && <Alert className="mt-3" type="error" showIcon message={photoError} />}
          {photoReconcile && (
            <Alert
              className="mt-3"
              type={photoReconcile.eligible_tickets > 0 ? 'warning' : 'success'}
              showIcon
              message={photoReconcile.repaired_tickets > 0
                ? `已修复 ${photoReconcile.repaired_tickets} 张遗漏工单`
                : photoReconcile.eligible_tickets > 0
                  ? `发现 ${photoReconcile.eligible_tickets} 张遗漏工单`
                  : '没有发现可自动修复的遗漏工单'}
              description={(
                <div className="flex flex-wrap items-center gap-3">
                  <span>需复制附件 {photoReconcile.attachment_copies} 份</span>
                  <span>已有附件 {photoReconcile.already_attached} 份</span>
                  {photoReconcile.manual_review_tickets > 0 && (
                    <span>待申请人补充 {photoReconcile.manual_review_tickets} 张，保留人工处理</span>
                  )}
                  {photoReconcile.missing_source_files > 0 && (
                    <span>缺少可复制原文件 {photoReconcile.missing_source_files} 份</span>
                  )}
                  {photoReconcile.eligible_tickets > 0 && photoReconcile.repaired_tickets === 0 && (
                    <Button
                      type="primary"
                      size="small"
                      loading={photoLoading}
                      onClick={handlePhotoReconcileConfirm}
                    >确认修复</Button>
                  )}
                </div>
              )}
            />
          )}
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

    </div>
  )
}
