import { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Modal,
  Progress,
  Tabs,
} from 'antd'
import PoliceDispatchPanel from '../components/PoliceDispatchPanel'
import PropertyAnnotationImport from '../components/PropertyAnnotationImport'
import VisitSourcePanel from '../components/VisitSourcePanel'
import VenueCloudPullControl from '../components/VenueCloudPullControl'
import CodeSummaryFetchControl from '../components/CodeSummaryFetchControl'
import ExternalAcquisitionScheduleControls from '../components/ExternalAcquisitionScheduleControls'
import AnalysisImportPanel from '../components/AnalysisImportPanel'
import QmfStatusScanControl from '../components/QmfStatusScanControl'
import RegistryManagement from './RegistryManagement'
import TxDocsMonitorSettings from './TxDocsMonitorSettings'
import { PageHeader, Panel } from '../components/ui'
import { useAuth } from '../context/AuthContext'
import { useSearchParams } from 'react-router-dom'
import {
  formatUTCTime,
  getExternalAcquisitionRun,
  getLatestExternalAcquisitionRun,
  startQmfSourceSync,
  startResidencePlatformScan,
  registryApi,
} from '../api/client'

export default function DataUploadCenter() {
  const { user, systemTimezone } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const activeTab = searchParams.get('tab') === 'external' ? 'external' : 'files'
  const activePanel = searchParams.get('panel')
  const canManagePoliceDispatch = Boolean(user?.permissions.includes('police.dispatch.manage'))
  const canManageQmfSource = Boolean(user?.permissions.includes('sync.trigger'))
  const canManageQmfScan = Boolean(user?.permissions.includes('qmf.registration.execute'))
  const canManageCertificates = Boolean(user?.permissions.includes('registry.import.manage'))
  const canManageProperties = Boolean(user?.permissions.includes('registry.property.manage'))
  const canManageAnalysis = Boolean(user?.permissions.includes('online.task.manage'))
  const canManageVisits = Boolean(user?.permissions.includes('visit.source.manage'))
  const canManageVenue = Boolean(user?.permissions.includes('venue.manage'))
  const canManageResidence = user?.role === 'super_admin'
    || user?.permission_groups?.some(group => group.code === 'super_admin')
    || user?.permission_group?.code === 'super_admin'
  const canUseUploadCenter = canManagePoliceDispatch || canManageQmfSource || canManageQmfScan || canManageCertificates
    || canManageResidence || canManageProperties || canManageAnalysis || canManageVisits || canManageVenue
  const [qmfRun, setQmfRun] = useState<import('../api/client').ExternalAcquisitionRun | null>(null)
  const [qmfLoading, setQmfLoading] = useState(false)
  const [certificateRun, setCertificateRun] = useState<Awaited<ReturnType<typeof registryApi.latestCertificateSourceRun>>['data']>(null)
  const [residenceRun, setResidenceRun] = useState<import('../api/client').ExternalAcquisitionRun | null>(null)

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

  const handleCertificateRetry = async (restart: boolean) => {
    if (!certificateRun) return
    try {
      setCertificateRun(await registryApi.retryCertificateSourceRun(certificateRun.id, restart))
    } catch (error: any) {
      Modal.error({ title: '责任告知书读取恢复失败', content: error?.response?.data?.detail || '请稍后重试' })
    }
  }

  const handleResidenceRead = async () => {
    try { const response = await startResidencePlatformScan(); setResidenceRun(response.run) } catch (error: any) { Modal.error({ title: '居住证读取启动失败', content: error?.response?.data?.detail || '请稍后重试' }) }
  }

  return (
    <div className="app-page min-w-0">
      {activePanel !== 'registry' && <PageHeader
        title="数据上传中心"
        description="统一管理业务文件导入与外部只读数据读取"
      />}

      {!canUseUploadCenter && (
        <Alert
          type="info"
          showIcon
          message="当前账号没有上传权限"
          description="当前账号没有可用的数据上传权限。"
        />
      )}

      {activePanel !== 'registry' && <Tabs
        activeKey={activeTab}
        onChange={tab => {
          const next = new URLSearchParams(searchParams)
          if (tab === 'files') next.delete('tab')
          else next.set('tab', tab)
          setSearchParams(next, { replace: true })
        }}
        items={[
          { key: 'files', label: '上传文件' },
          { key: 'external', label: '读取外部' },
        ]}
      />}

      {activePanel === 'registry' && activeTab === 'files' && canManageCertificates && (
        <div className="grid gap-3">
          <Button className="justify-self-start" onClick={() => setSearchParams({ tab: 'files' }, { replace: true })}>返回上传文件</Button>
          <RegistryManagement importCenterMode />
        </div>
      )}
      {activePanel === 'analysis' && activeTab === 'files' && canManageAnalysis && (
        <div className="grid gap-3">
          <Button className="justify-self-start" onClick={() => setSearchParams({ tab: 'files' }, { replace: true })}>返回上传文件</Button>
          <Panel title="研判结果工作簿"><AnalysisImportPanel /></Panel>
        </div>
      )}
      {activePanel === 'txdocs' && activeTab === 'external' && canManageResidence && (
        <div className="grid gap-3">
          <Button className="justify-self-start" onClick={() => setSearchParams({ tab: 'external' }, { replace: true })}>返回读取外部</Button>
          <TxDocsMonitorSettings />
        </div>
      )}

      {activePanel !== 'registry' && activePanel !== 'txdocs' && activeTab === 'external' && (canManageCertificates || canManageResidence || canManageQmfSource || canManageQmfScan || canManageVisits || canManageVenue) && (
        <div className="grid gap-4">
        {canManageResidence && <Panel title="自动任务编排" description="仅管理已有安全调度的启停和周期；凭据仍在系统设置中维护。"><ExternalAcquisitionScheduleControls /></Panel>}
        {canManageResidence && <Panel title="腾讯只读统计监控" description="该通道只产生去正文的变化统计，不导入本地任务，也不向腾讯写回。"><Button onClick={() => setSearchParams({ tab: 'external', panel: 'txdocs' })}>管理监控目标和间隔</Button></Panel>}
        <Panel title="外部数据读取" description="从统一入口查看来源任务并发起已有的只读读取流程；各来源权限、确认和运行边界不变。">
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
            {canManageCertificates && certificateRun?.status === 'failed' && <div className="flex flex-wrap gap-2 rounded-md border border-[var(--app-border)] p-3">
              {certificateRun.error_code !== 'source_changed' && <Button onClick={() => void handleCertificateRetry(false)}>继续读取</Button>}
              <Button onClick={() => Modal.confirm({ title: '从第一页重新读取告知书？', content: '已保存分页进度会被清除，不影响上一次成功的预览。', okText: '重新读取', cancelText: '取消', onOk: () => handleCertificateRetry(true) })}>从头重读</Button>
            </div>}
            {canManageResidence && <div className="rounded-lg border border-[var(--app-border)] p-3"><div className="flex items-center justify-between gap-2"><strong>居住证平台</strong><Button size="small" type="primary" onClick={() => void handleResidenceRead()} disabled={Boolean(residenceRun && ['queued', 'running'].includes(residenceRun.status))}>读取</Button></div><div className="mt-2 text-xs text-[var(--app-text-secondary)]">{residenceRun?.message || '尚未读取'}</div>{residenceRun?.progress !== null && residenceRun && <Progress percent={residenceRun.progress || 0} size="small" />}</div>}
            {canManageQmfSource && <div className="rounded-lg border border-[var(--app-border)] p-3"><div className="flex items-center justify-between gap-2"><strong>全民防平台</strong><Button size="small" type="primary" loading={qmfLoading} onClick={() => void handleQmfSync()} disabled={Boolean(qmfRun && ['queued', 'running'].includes(qmfRun.status))}>读取</Button></div><div className="mt-2 text-xs text-[var(--app-text-secondary)]">{qmfRun?.message || '尚未读取'}</div>{qmfRun?.progress !== null && qmfRun && <Progress percent={qmfRun.progress || 0} size="small" status={qmfRun.status === 'failed' ? 'exception' : qmfRun.status === 'success' || qmfRun.status === 'warning' ? 'success' : 'active'} />}{qmfRun?.created_at && <div className="mt-2 text-xs text-[var(--app-text-secondary)]">最近任务：{qmfRun.status === 'success' ? '已完成' : qmfRun.status === 'warning' ? '完成但有提醒' : qmfRun.status === 'failed' ? '失败' : qmfRun.status === 'running' ? '处理中' : '排队中'} · {formatUTCTime(qmfRun.created_at, systemTimezone)}</div>}{qmfRun?.error_message && <Alert className="mt-2" type="error" showIcon message={qmfRun.error_message} />}{qmfRun?.result && <div className="mt-2 flex flex-wrap gap-3 text-xs text-[var(--app-text-secondary)]"><span>读取 {qmfRun.result.record_count ?? 0} 条</span><span>有效 {qmfRun.result.valid_count ?? 0} 条</span><span>社区未匹配 {qmfRun.result.unresolved_count ?? 0} 条</span><span>来源问题 {qmfRun.result.issue_count ?? 0} 条</span></div>}</div>}
          </div>
        </Panel>
        {canManageVisits && <>
          <Panel title="平安码与管家码" description="按业务日期读取外部来源并更新本地快照；读取失败会保留最近成功快照。"><CodeSummaryFetchControl /></Panel>
          <VisitSourcePanel />
        </>}
        {canManageQmfScan && <Panel title="全民防反馈状态核对" description="对已完成模型三任务执行受控只读核对；扫描结果继续显示在模型三任务列表。"><QmfStatusScanControl /></Panel>}
        {canManageVenue && <Panel title="场所云端饮酒报备" description="读取云端待处理队列并写入本地业务库；云端仅作数据来源。"><VenueCloudPullControl /></Panel>}
        </div>
      )}

      {!(activePanel === 'registry' && canManageCertificates) && !(activePanel === 'analysis' && canManageAnalysis) && activeTab === 'files' && <div className="grid gap-4">
        <PoliceDispatchPanel enabled={canManagePoliceDispatch} />
        {canManageAnalysis && <Panel title="结构化研判结果" description="导入平台导出的待研判工作簿，服务端会逐行核对任务状态与版本。">
          <Button onClick={() => setSearchParams({ tab: 'files', panel: 'analysis' })}>导入研判结果</Button>
        </Panel>}
        {canManageCertificates && <Panel title="房屋档案导入" description="户号表预览、告知书关联确认和问题核查使用档案导入权限。">
          <Button type="primary" onClick={() => setSearchParams({ tab: 'files', panel: 'registry' })}>打开房屋档案导入</Button>
        </Panel>}
        {canManageProperties && <Panel title="小区标注工作簿" description="导入标注建议后逐项确认；地址和社区原值不会被工作簿修改。"><PropertyAnnotationImport onChanged={async () => undefined} /></Panel>}
      </div>}
    </div>
  )
}
