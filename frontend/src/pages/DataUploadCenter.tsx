import { Alert, Button, Tabs } from 'antd'
import { useSearchParams } from 'react-router-dom'
import PoliceDispatchPanel from '../components/PoliceDispatchPanel'
import PropertyAnnotationImport from '../components/PropertyAnnotationImport'
import AnalysisImportPanel from '../components/AnalysisImportPanel'
import ExternalAcquisitionWorkbench from '../components/ExternalAcquisitionWorkbench'
import RegistryManagement from './RegistryManagement'
import TxDocsMonitorSettings from './TxDocsMonitorSettings'
import { PageHeader, Panel } from '../components/ui'
import { useAuth } from '../context/AuthContext'

export default function DataUploadCenter() {
  const { user } = useAuth()
  const [searchParams, setSearchParams] = useSearchParams()
  const activeTab = searchParams.get('tab') === 'external' ? 'external' : 'files'
  const activePanel = searchParams.get('panel')
  const canManagePoliceDispatch = Boolean(user?.permissions.includes('police.dispatch.manage'))
  const canManageCertificates = Boolean(user?.permissions.includes('registry.import.manage'))
  const canManageProperties = Boolean(user?.permissions.includes('registry.property.manage'))
  const canManageAnalysis = Boolean(user?.permissions.includes('online.task.manage'))
  const canManageExternal = Boolean(user?.permissions.some(permission => ['sync.trigger', 'qmf.registration.execute', 'visit.source.manage', 'venue.manage'].includes(permission)))
  const canManageTxDocs = user?.role === 'super_admin' || user?.permission_groups?.some(group => group.code === 'super_admin') || user?.permission_group?.code === 'super_admin'
  const canUseUploadCenter = canManagePoliceDispatch || canManageCertificates || canManageProperties || canManageAnalysis || canManageExternal || canManageTxDocs

  return <div className="app-page min-w-0">
    {activePanel !== 'registry' && <PageHeader title="数据上传中心" description="统一管理业务文件导入与外部只读数据读取" />}
    {!canUseUploadCenter && <Alert type="info" showIcon message="当前账号没有上传权限" description="当前账号没有可用的数据上传权限。" />}
    {activePanel !== 'registry' && activePanel !== 'analysis' && activePanel !== 'txdocs' && <Tabs activeKey={activeTab} onChange={tab => { const next = new URLSearchParams(searchParams); if (tab === 'files') next.delete('tab'); else next.set('tab', tab); next.delete('source'); setSearchParams(next, { replace: true }) }} items={[{ key: 'files', label: '上传文件' }, { key: 'external', label: '读取外部' }]} />}

    {activePanel === 'registry' && activeTab === 'files' && canManageCertificates && <div className="grid gap-3"><Button className="justify-self-start" onClick={() => setSearchParams({ tab: 'files' }, { replace: true })}>返回上传文件</Button><RegistryManagement importCenterMode /></div>}
    {activePanel === 'analysis' && activeTab === 'files' && canManageAnalysis && <div className="grid gap-3"><Button className="justify-self-start" onClick={() => setSearchParams({ tab: 'files' }, { replace: true })}>返回上传文件</Button><Panel title="研判结果工作簿"><AnalysisImportPanel /></Panel></div>}
    {activePanel === 'txdocs' && activeTab === 'external' && canManageTxDocs && <div className="grid gap-3"><Button className="justify-self-start" onClick={() => setSearchParams({ tab: 'external' }, { replace: true })}>返回读取外部</Button><TxDocsMonitorSettings /></div>}

    {activeTab === 'external' && activePanel !== 'txdocs' && <div className="grid gap-3"><Alert type="info" showIcon message="自动任务默认关闭" description="外部来源按来源独立管理。自动提交只接受完整、无冲突且通过安全门禁的结果；腾讯只读监控不进入业务写入链路。" /><ExternalAcquisitionWorkbench /></div>}

    {!activePanel && activeTab === 'files' && <div className="grid gap-4">
      <PoliceDispatchPanel enabled={canManagePoliceDispatch} />
      {canManageAnalysis && <Panel title="结构化研判结果" description="导入平台导出的待研判工作簿，服务端会逐行核对任务状态与版本。"><Button onClick={() => setSearchParams({ tab: 'files', panel: 'analysis' })}>导入研判结果</Button></Panel>}
      {canManageCertificates && <Panel title="房屋档案导入" description="户号表预览、告知书关联确认和问题核查使用档案导入权限。"><Button type="primary" onClick={() => setSearchParams({ tab: 'files', panel: 'registry' })}>打开房屋档案导入</Button></Panel>}
      {canManageProperties && <Panel title="小区标注工作簿" description="导入标注建议后逐项确认；地址和社区原值不会被工作簿修改。"><PropertyAnnotationImport onChanged={async () => undefined} /></Panel>}
    </div>}
  </div>
}
