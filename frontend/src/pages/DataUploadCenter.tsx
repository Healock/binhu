import { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Modal,
  Progress,
  Upload,
} from 'antd'
import type { UploadFile, UploadProps } from 'antd'
import { UploadOutlined } from '@ant-design/icons'
import PoliceDispatchPanel from '../components/PoliceDispatchPanel'
import { PageHeader, Panel } from '../components/ui'
import { useAuth } from '../context/AuthContext'
import {
  formatUTCTime,
  getExternalAcquisitionRun,
  getLatestExternalAcquisitionRun,
  getLatestModelThreeSelfOwnedRoster,
  importModelThreeSelfOwnedRoster,
  startQmfSourceSync,
  startResidencePlatformScan,
  registryApi,
  type SelfOwnedRosterResult,
} from '../api/client'

const MAX_SELF_OWNED_ZIP_BYTES = 100 * 1024 * 1024

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
  const canManageQmfSource = Boolean(user?.permissions.includes('sync.trigger'))
  const canManageCertificates = Boolean(user?.permissions.includes('registry.import.manage'))
  const canManageResidence = user?.role === 'super_admin'
    || user?.permission_groups?.some(group => group.code === 'super_admin')
    || user?.permission_group?.code === 'super_admin'
  const canUseUploadCenter = canManagePoliceDispatch || canManageQmfSource || canManageCertificates || canManageResidence
  const [qmfRun, setQmfRun] = useState<import('../api/client').ExternalAcquisitionRun | null>(null)
  const [qmfLoading, setQmfLoading] = useState(false)
  const [certificateRun, setCertificateRun] = useState<Awaited<ReturnType<typeof registryApi.latestCertificateSourceRun>>['data']>(null)
  const [residenceRun, setResidenceRun] = useState<import('../api/client').ExternalAcquisitionRun | null>(null)
  const [selfOwnedFile, setSelfOwnedFile] = useState<File | null>(null)
  const [selfOwnedFileList, setSelfOwnedFileList] = useState<UploadFile[]>([])
  const [selfOwnedResult, setSelfOwnedResult] = useState<SelfOwnedRosterResult | null>(null)
  const [selfOwnedLoading, setSelfOwnedLoading] = useState(false)
  const [selfOwnedError, setSelfOwnedError] = useState('')

  const loadQmfRun = async () => {
    if (!canManageQmfSource) return
    try {
      const latest = await getLatestExternalAcquisitionRun('qmf_source')
      if (latest) setQmfRun(latest)
    } catch {
      // 外部只读扫描入口仍可使用；任务状态读取失败不阻断页面其他上传功能。
    }
  }

  useEffect(() => { void loadQmfRun() }, [canManageQmfSource])
  useEffect(() => {
    if (canManageCertificates) void registryApi.latestCertificateSourceRun().then(result => setCertificateRun(result.data)).catch(() => undefined)
    if (canManageResidence) void getLatestExternalAcquisitionRun('residence_full_scan').then(setResidenceRun).catch(() => undefined)
  }, [canManageCertificates, canManageResidence])
  useEffect(() => {
    if (!certificateRun || !['pending', 'running'].includes(certificateRun.status)) return
    const timer = window.setInterval(() => { void registryApi.certificateSourceRun(certificateRun.id).then(setCertificateRun).catch(() => undefined) }, 1500)
    return () => window.clearInterval(timer)
  }, [certificateRun?.id, certificateRun?.status])
  useEffect(() => {
    if (!residenceRun || !['queued', 'running'].includes(residenceRun.status)) return
    const timer = window.setInterval(() => { void getExternalAcquisitionRun(residenceRun.id).then(setResidenceRun).catch(() => undefined) }, 1500)
    return () => window.clearInterval(timer)
  }, [residenceRun?.id, residenceRun?.status])
  useEffect(() => {
    if (!canManageQmfSource) return
    void getLatestModelThreeSelfOwnedRoster().then(setSelfOwnedResult).catch(() => undefined)
  }, [canManageQmfSource])
  useEffect(() => {
    if (!canManageQmfSource || !qmfRun || !['queued', 'running'].includes(qmfRun.status)) return
    const timer = window.setInterval(() => {
      void getExternalAcquisitionRun(qmfRun.id).then(setQmfRun).catch(() => undefined)
    }, 1500)
    return () => window.clearInterval(timer)
  }, [canManageQmfSource, qmfRun?.id, qmfRun?.status])

  const handleQmfSync = async () => {
    setQmfLoading(true)
    try {
      const response = await startQmfSourceSync()
      setQmfRun(response.data)
    } catch (error: any) {
          Modal.error({ title: '全民防只读扫描启动失败', content: error?.response?.data?.detail || '请稍后重试' })
    } finally {
      setQmfLoading(false)
    }
  }

  const handleCertificateRead = async () => {
    try { setCertificateRun(await registryApi.startCertificateSourceRun()) } catch (error: any) { Modal.error({ title: '责任告知书读取启动失败', content: error?.response?.data?.detail || '请稍后重试' }) }
  }

  const handleResidenceRead = async () => {
    try { const response = await startResidencePlatformScan(); setResidenceRun(response.run) } catch (error: any) { Modal.error({ title: '居住证读取启动失败', content: error?.response?.data?.detail || '请稍后重试' }) }
  }

  const beforeSelfOwnedUpload: UploadProps['beforeUpload'] = file => {
    setSelfOwnedError('')
    setSelfOwnedResult(null)
    if (!file.name.toLowerCase().endsWith('.zip')) {
      setSelfOwnedError('自购自住名单只支持 ZIP 文件')
      return Upload.LIST_IGNORE
    }
    if (file.size > MAX_SELF_OWNED_ZIP_BYTES) {
      setSelfOwnedError('自购自住名单 ZIP 不能超过 100MB')
      return Upload.LIST_IGNORE
    }
    setSelfOwnedFile(file)
    setSelfOwnedFileList([selectedUploadFile(file)])
    return false
  }

  const handleSelfOwnedImport = async () => {
    if (!selfOwnedFile) return
    setSelfOwnedLoading(true)
    setSelfOwnedError('')
    try {
      setSelfOwnedResult(await importModelThreeSelfOwnedRoster(selfOwnedFile))
      setSelfOwnedFile(null)
      setSelfOwnedFileList([])
    } catch (error: any) {
      const status = error?.response?.status
      const timedOut = error?.code === 'ECONNABORTED'
        || error?.code === 'ETIMEDOUT'
        || status === 408
        || status === 504
      const uploadError = status === 413
        ? '自购自住名单 ZIP 超过服务器上传限制，请联系管理员检查网关配置'
        : timedOut
          ? '自购自住名单上传或处理超时；已先查询最近一次结果，请勿重复上传相同文件'
          : error?.response?.data?.detail || '自购自住名单导入失败，请稍后重试'
      if (timedOut && canManageQmfSource) {
        void getLatestModelThreeSelfOwnedRoster().then(setSelfOwnedResult).catch(() => undefined)
      }
      setSelfOwnedError(
        uploadError,
      )
    } finally {
      setSelfOwnedLoading(false)
    }
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

      {(canManageCertificates || canManageResidence || canManageQmfSource) && (
        <Panel title="外部数据读取调度" description="责任告知书、居住证和全民防读取统一显示任务进度；各来源仍按原有权限和只读边界执行。">
          <div className="grid gap-3 md:grid-cols-3">
            {canManageCertificates && <div className="rounded-lg border border-[var(--app-border)] p-3">
              <div className="flex items-center justify-between gap-2"><strong>责任告知书</strong><Button size="small" type="primary" onClick={() => void handleCertificateRead()} disabled={Boolean(certificateRun && ['pending', 'running'].includes(certificateRun.status))}>读取</Button></div>
              <div className="mt-2 text-xs text-[var(--app-text-secondary)]">{certificateRun ? `第 ${certificateRun.current_page}/${certificateRun.total_pages || '?'} 页 · ${certificateRun.fetched_count} 条` : '尚未读取'}</div>
              {certificateRun?.total_pages && <Progress percent={Math.min(100, Math.round(certificateRun.current_page * 100 / certificateRun.total_pages))} size="small" />}
              {certificateRun?.preview?.progress && <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-xs text-[var(--app-text-secondary)]">
                <span>已读取 {certificateRun.preview.progress.incoming_total ?? certificateRun.accepted_count} 条</span>
                <span>范围通过 {certificateRun.preview.progress.scope_accepted ?? certificateRun.accepted_count} 条</span>
                <span>范围外 {certificateRun.preview.progress.scope_rejected ?? certificateRun.rejected_count} 条</span>
              </div>}
              {certificateRun?.preview?.progress?.status_counts && <div className="mt-1 text-xs">状态：{Object.entries(certificateRun.preview.progress.status_counts).map(([key, value]) => `${key} ${value}`).join(' · ')}</div>}
            </div>}
            {canManageResidence && <div className="rounded-lg border border-[var(--app-border)] p-3"><div className="flex items-center justify-between gap-2"><strong>居住证平台</strong><Button size="small" type="primary" onClick={() => void handleResidenceRead()} disabled={Boolean(residenceRun && ['queued', 'running'].includes(residenceRun.status))}>读取</Button></div><div className="mt-2 text-xs text-[var(--app-text-secondary)]">{residenceRun?.message || '尚未读取'}</div>{residenceRun?.progress !== null && residenceRun && <Progress percent={residenceRun.progress || 0} size="small" />}</div>}
            {canManageQmfSource && <div className="rounded-lg border border-[var(--app-border)] p-3"><div className="flex items-center justify-between gap-2"><strong>全民防平台</strong><Button size="small" type="primary" loading={qmfLoading} onClick={() => void handleQmfSync()} disabled={Boolean(qmfRun && ['queued', 'running'].includes(qmfRun.status))}>读取</Button></div><div className="mt-2 text-xs text-[var(--app-text-secondary)]">{qmfRun?.message || '尚未读取'}</div>{qmfRun?.progress !== null && qmfRun && <Progress percent={qmfRun.progress || 0} size="small" status={qmfRun.status === 'failed' ? 'exception' : qmfRun.status === 'success' || qmfRun.status === 'warning' ? 'success' : 'active'} />}{qmfRun?.created_at && <div className="mt-2 text-xs text-[var(--app-text-secondary)]">最近任务：{qmfRun.status === 'success' ? '已完成' : qmfRun.status === 'warning' ? '完成但有提醒' : qmfRun.status === 'failed' ? '失败' : qmfRun.status === 'running' ? '处理中' : '排队中'} · {formatUTCTime(qmfRun.created_at, systemTimezone)}</div>}{qmfRun?.error_message && <Alert className="mt-2" type="error" showIcon message={qmfRun.error_message} />}{qmfRun?.result && <div className="mt-2 flex flex-wrap gap-3 text-xs text-[var(--app-text-secondary)]"><span>读取 {qmfRun.result.record_count ?? 0} 条</span><span>有效 {qmfRun.result.valid_count ?? 0} 条</span><span>社区未匹配 {qmfRun.result.unresolved_count ?? 0} 条</span><span>来源问题 {qmfRun.result.issue_count ?? 0} 条</span></div>}</div>}
          </div>
        </Panel>
      )}

      {canManageQmfSource && (
        <Panel
          title="辖区资产：自购自住人员"
          description="用于维护辖区内自购自住房屋居住人员基础资料。姓名、身份证号和个人联系电话会进入受权限保护的人员档案，并自动挂上“自购自住”标签；当前及后续匹配的模型三空白或无法核实结果会填为“近期返吴”，不会调用全民防写入接口。"
        >
          <Upload
            accept=".zip"
            maxCount={1}
            fileList={selfOwnedFileList}
            beforeUpload={beforeSelfOwnedUpload}
            onRemove={() => {
              setSelfOwnedFile(null)
              setSelfOwnedFileList([])
              setSelfOwnedError('')
            }}
            disabled={selfOwnedLoading}
          >
            <Button icon={<UploadOutlined />} disabled={selfOwnedLoading}>选择自购自住名单 ZIP</Button>
          </Upload>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button
              type="primary"
              loading={selfOwnedLoading}
              disabled={!selfOwnedFile || selfOwnedLoading}
              onClick={() => void handleSelfOwnedImport()}
            >导入并更新平台核查结果</Button>
            <span className="text-xs text-[var(--app-text-secondary)]">按居民证号去重，原始 ZIP 不落库；匹配使用身份 HMAC</span>
          </div>
          {selfOwnedError && <Alert className="mt-3" type="error" showIcon message={selfOwnedError} />}
          {selfOwnedResult && (
            <div className="mt-3 rounded-lg border border-[var(--app-border)] p-3 text-sm">
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                {selfOwnedResult.upgraded && <span className="font-medium text-blue-600">旧批次已按新规则补齐</span>}
                <span>文件 {selfOwnedResult.workbook_count} 个</span>
                <span>记录 {selfOwnedResult.total_rows} 条</span>
                <span>有效 {selfOwnedResult.valid_rows} 条</span>
                <span>无效 {selfOwnedResult.invalid_rows} 条</span>
                <span>重复 {selfOwnedResult.duplicate_rows} 条</span>
                {selfOwnedResult.registry_people_created !== undefined && (
                  <span>新增人员档案 {selfOwnedResult.registry_people_created} 条</span>
                )}
                {selfOwnedResult.registry_people_reused !== undefined && (
                  <span>复用人员档案 {selfOwnedResult.registry_people_reused} 条</span>
                )}
                {selfOwnedResult.registry_phones_created !== undefined && (
                  <span>补充联系电话 {selfOwnedResult.registry_phones_created} 条</span>
                )}
                {selfOwnedResult.tag_assignments_created !== undefined && (
                  <span>新增自购自住标签 {selfOwnedResult.tag_assignments_created} 条</span>
                )}
                <span>命中模型三 {selfOwnedResult.matched_tasks} 条</span>
                <span className="font-medium text-green-600">已填近期返吴 {selfOwnedResult.updated_tasks} 条</span>
                <span>保留原结果 {selfOwnedResult.skipped_tasks} 条</span>
              </div>
            </div>
          )}
        </Panel>
      )}

      <PoliceDispatchPanel enabled={canManagePoliceDispatch} />
    </div>
  )
}
