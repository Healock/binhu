import { Alert, Button, Input, InputNumber, Progress, Switch, Upload, message } from 'antd'
import { ArrowLeftOutlined, InboxOutlined, ToolOutlined } from '@ant-design/icons'
import type { UploadFile, UploadProps } from 'antd'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Panel } from '../components/ui'
import { getResidencePlatformConfig } from '../api/client'
import { downloadBlob } from '../utils/fileDownload'
import {
  OfflineResidenceClient,
  cacheOnlineResidenceConfig,
  loadOfflineResidenceConfig,
  normalizeMacAddress,
  pushMacAddress,
  readMacAddress,
  saveOfflineResidenceConfig,
  type OfflineResidenceConfig,
} from '../utils/offlineResidenceClient'
import { readOfflineWorkbook, writeOfflineWorkbook, type OfflineWorkbook } from '../utils/offlineResidenceXlsx'

const { Dragger } = Upload
type QueryState = 'idle' | 'running' | 'completed' | 'partial' | 'failed'

function configIsUsable(config: OfflineResidenceConfig): boolean {
  return Boolean(
    config.enabled
    && config.base_url.trim()
    && config.password
    && config.mac_service_url.trim()
    && config.accounts.length > 0
    && config.accounts.every(account => account.username.trim()),
  )
}

export default function OfflineMode() {
  const navigate = useNavigate()
  const [config, setConfig] = useState<OfflineResidenceConfig>(() => loadOfflineResidenceConfig())
  const [file, setFile] = useState<File | null>(null)
  const [fileList, setFileList] = useState<UploadFile[]>([])
  const [workbook, setWorkbook] = useState<OfflineWorkbook | null>(null)
  const [statuses, setStatuses] = useState<string[]>([])
  const [queryState, setQueryState] = useState<QueryState>('idle')
  const [completed, setCompleted] = useState(0)
  const [successCount, setSuccessCount] = useState(0)
  const [error, setError] = useState('')
  const [configMessage, setConfigMessage] = useState('')
  const [exporting, setExporting] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [macBusy, setMacBusy] = useState(false)
  const [macMessage, setMacMessage] = useState('')
  const [macMessageType, setMacMessageType] = useState<'info' | 'success' | 'error'>('info')

  const running = queryState === 'running'
  const total = workbook?.rows.length ?? 0
  const configWarning = useMemo(() => {
    if (!config.enabled) return '离线居住证查询已关闭。'
    if (!config.base_url.trim() || !config.mac_service_url.trim()) return '请填写居住证接口地址和 MAC 服务地址。'
    if (!config.password) return '未填写统一登录密码。在线平台不会回传密码，请在此处手动填写。'
    if (!config.accounts.length) return '没有可用的社区账号配置，请先刷新在线范围或在本机填写账号。'
    const incomplete = config.accounts.filter(account => !account.username.trim())
    if (incomplete.length) return `请填写完整居住证登录账号：${incomplete.map(account => account.community_name || '本地账号').join('、')}`
    return ''
  }, [config])

  const updateConfig = (change: Partial<OfflineResidenceConfig>) => {
    setConfig(current => ({ ...current, ...change }))
    setConfigMessage('')
    setMacMessage('')
  }

  const persistConfig = () => {
    const next = {
      ...config,
      base_url: config.base_url.trim().replace(/\/+$/, ''),
      mac_service_url: config.mac_service_url.trim().replace(/\/+$/, ''),
      timeout_seconds: Math.min(120, Math.max(1, Number(config.timeout_seconds) || 15)),
    }
    saveOfflineResidenceConfig(next)
    setConfig(next)
    setConfigMessage('离线配置已保存到当前客户端。密码不会上传到滨湖平台。')
  }

  const readCurrentMac = async () => {
    setMacBusy(true)
    setMacMessage('')
    try {
      const mac = await readMacAddress(config)
      const next = { ...config, mac_address: mac }
      saveOfflineResidenceConfig(next)
      setConfig(next)
      setMacMessage(`当前 MAC：${mac}`)
      setMacMessageType('info')
    } catch (reason) {
      setMacMessage(reason instanceof Error ? reason.message : '读取 MAC 失败')
      setMacMessageType('error')
    } finally {
      setMacBusy(false)
    }
  }

  const pushConfiguredMac = async () => {
    setMacBusy(true)
    setMacMessage('')
    try {
      const mac = normalizeMacAddress(config.mac_address)
      const verified = await pushMacAddress(config, mac, config.mac_write_token)
      const next = { ...config, mac_address: verified }
      saveOfflineResidenceConfig(next)
      setConfig(next)
      setMacMessage(`MAC 已保存并回读确认：${verified}`)
      setMacMessageType('success')
    } catch (reason) {
      setMacMessage(reason instanceof Error ? reason.message : '保存 MAC 失败')
      setMacMessageType('error')
    } finally {
      setMacBusy(false)
    }
  }

  const syncOnlineConfig = async () => {
    setSyncing(true)
    setConfigMessage('')
    try {
      const online = await getResidencePlatformConfig()
      const next = cacheOnlineResidenceConfig(online)
      setConfig(next)
      if (!online.base_url?.trim() || !online.mac_service_url?.trim()) {
        setConfigMessage('在线配置尚不完整，已保留当前客户端的离线配置。')
      } else setConfigMessage(online.password_configured && !next.password
        ? '已同步接口、MAC、超时和选中社区范围；账号和统一密码不会从平台返回，请在当前客户端手动填写。'
        : '已同步接口、MAC、超时和选中社区范围；本地已有账号和密码已保留。')
    } catch {
      setConfigMessage('无法连接滨湖平台，未同步在线配置；可以直接手动修改离线配置。')
    } finally {
      setSyncing(false)
    }
  }

  const beforeUpload: UploadProps['beforeUpload'] = selected => {
    if (!selected.name.toLowerCase().endsWith('.xlsx')) {
      message.error('只支持 .xlsx 文件')
      return Upload.LIST_IGNORE
    }
    setFile(selected)
    setFileList([{ uid: selected.uid, name: selected.name, size: selected.size, status: 'done', originFileObj: selected }])
    setWorkbook(null)
    setStatuses([])
    setCompleted(0)
    setSuccessCount(0)
    setQueryState('idle')
    setError('')
    return false
  }

  const start = async () => {
    if (!file) return
    setError('')
    setQueryState('running')
    setCompleted(0)
    setSuccessCount(0)
    try {
      const book = await readOfflineWorkbook(file)
      if (!book.rows.length) throw new Error('文件中没有可处理的数据行')
      setWorkbook(book)
      const nextStatuses = Array.from({ length: book.rows.length }, () => '查询中')
      setStatuses(nextStatuses)
      const client = new OfflineResidenceClient(config)
      let completedCount = 0
      let successfulCount = 0
      const results = [...nextStatuses]
      const concurrency = Math.min(4, Math.max(1, book.rows.length))
      let cursor = 0
      const worker = async () => {
        while (cursor < book.rows.length) {
          const index = cursor
          cursor += 1
          const identity = String(book.rows[index]?.[book.identityColumn] ?? '')
          let result: { status: string; error?: string }
          try {
            result = await client.lookup(identity)
          } catch {
            result = { status: '查询失败', error: 'request_error' }
          }
          results[index] = result.status
          if (!result.error) successfulCount += 1
          completedCount += 1
          setStatuses([...results])
          setCompleted(completedCount)
          setSuccessCount(successfulCount)
        }
      }
      await Promise.all(Array.from({ length: concurrency }, worker))
      setQueryState(successfulCount === book.rows.length ? 'completed' : 'partial')
    } catch (reason) {
      setQueryState('failed')
      setError(reason instanceof Error ? reason.message : '名单读取或批量查询失败')
    }
  }

  const exportResult = async () => {
    if (!workbook || completed !== workbook.rows.length) return
    setExporting(true)
    try {
      const blob = writeOfflineWorkbook(workbook, statuses)
      const sourceName = file?.name.replace(/\.xlsx$/i, '') || '居住登记查询结果'
      await downloadBlob(blob, `${sourceName}-登记情况.xlsx`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '结果导出失败')
    } finally {
      setExporting(false)
    }
  }

  return (
    <main className="offline-mode-page h-full min-h-0 overflow-y-auto overscroll-contain bg-[var(--app-page-bg)] px-4 py-6 text-[var(--app-text-primary)] md:px-6 md:py-10">
      <div className="mx-auto max-w-4xl">
        <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/login')}>返回登录</Button>
        <div className="mt-6 grid gap-4">
          <section className="app-card p-8">
            <div className="flex items-start gap-4">
              <ToolOutlined className="mt-1 text-2xl text-[var(--app-primary)]" />
              <div>
                <h1 className="m-0 text-2xl font-semibold">离线模式</h1>
                <p className="mt-3 text-[var(--app-text-secondary)]">离线模式只表示滨湖平台服务器不可访问。查询时客户端直接访问居住证系统，不经过滨湖平台；名单只在本机处理。</p>
              </div>
            </div>
          </section>

          <Panel title="居住证系统配置" description="配置保存在当前客户端。在线平台同步接口、MAC 服务地址和超时；账号、统一密码、MAC 写入令牌和会话不会从平台配置接口回传。" extra={<Button onClick={() => void syncOnlineConfig()} loading={syncing}>刷新在线配置</Button>}>
            <div className="grid gap-4">
              {configWarning && <Alert type="warning" showIcon message={configWarning} />}
              {configMessage && <Alert type="info" showIcon message={configMessage} />}
              <div className="grid gap-4 md:grid-cols-2">
                <label className="settings-field text-sm text-[var(--app-text-strong)]"><span className="settings-field__label font-medium">离线查询开关</span><div className="flex min-h-9 items-center gap-3"><Switch checked={config.enabled} onChange={enabled => updateConfig({ enabled })} /><span>{config.enabled ? '已开启' : '已关闭'}</span></div></label>
                <label className="settings-field text-sm text-[var(--app-text-strong)]"><span className="settings-field__label font-medium">居住证接口地址</span><Input value={config.base_url} onChange={event => updateConfig({ base_url: event.target.value })} /></label>
                <label className="settings-field text-sm text-[var(--app-text-strong)]"><span className="settings-field__label font-medium">统一登录密码</span><Input.Password value={config.password} onChange={event => updateConfig({ password: event.target.value })} autoComplete="new-password" /></label>
                <label className="settings-field text-sm text-[var(--app-text-strong)]"><span className="settings-field__label font-medium">MAC 服务地址</span><Input value={config.mac_service_url} onChange={event => updateConfig({ mac_service_url: event.target.value })} /></label>
                <label className="settings-field text-sm text-[var(--app-text-strong)]"><span className="settings-field__label font-medium">要使用的 MAC 地址</span><Input value={config.mac_address} onChange={event => updateConfig({ mac_address: event.target.value })} placeholder="AA:BB:CC:DD:EE:FF" autoComplete="off" /></label>
                <label className="settings-field text-sm text-[var(--app-text-strong)]"><span className="settings-field__label font-medium">MAC 写入令牌（如服务端要求）</span><Input.Password value={config.mac_write_token} onChange={event => updateConfig({ mac_write_token: event.target.value })} placeholder="不会上传到滨湖平台" autoComplete="off" /></label>
                <label className="settings-field text-sm text-[var(--app-text-strong)]"><span className="settings-field__label font-medium">请求超时（秒）</span><InputNumber min={1} max={120} value={config.timeout_seconds} onChange={value => updateConfig({ timeout_seconds: Number(value || 15) })} className="w-full" /></label>
              </div>
              {config.login_community_names.length > 0 && <Alert type="info" showIcon message={`在线查询范围：${config.login_community_names.join('、')}`} />}
              <div className="grid gap-3">
                {!config.login_community_ids.length && <div className="flex justify-end"><Button onClick={() => updateConfig({ accounts: [...config.accounts, { community_id: null, community_name: '', username: '', community_code: '' }] })}>添加本地社区账号</Button></div>}
                {config.accounts.map((account, index) => <div key={account.community_id || `local-${index}`} className="grid gap-3 rounded-xl border border-[var(--app-border)] p-4 md:grid-cols-2">
                  <div className="flex items-center justify-between gap-3 md:col-span-2"><span className="text-sm font-medium text-[var(--app-text-strong)]">{account.community_name || `本地社区账号 ${index + 1}`}</span>{!config.login_community_ids.length && <Button size="small" danger onClick={() => updateConfig({ accounts: config.accounts.filter((_, itemIndex) => itemIndex !== index) })}>移除</Button>}</div>
                  <label className="settings-field text-sm text-[var(--app-text-strong)]"><span className="settings-field__label font-medium">完整登录账号</span><Input value={account.username} onChange={event => updateConfig({ accounts: config.accounts.map((item, itemIndex) => itemIndex === index ? { ...item, username: event.target.value } : item) })} autoComplete="username" /></label>
                  <label className="settings-field text-sm text-[var(--app-text-strong)]"><span className="settings-field__label font-medium">组织代码（可选）</span><Input value={account.community_code} onChange={event => updateConfig({ accounts: config.accounts.map((item, itemIndex) => itemIndex === index ? { ...item, community_code: event.target.value.toUpperCase() } : item) })} placeholder="接口未返回组织代码时使用" /></label>
                </div>)}
              </div>
              <div className="text-xs text-[var(--app-text-secondary)]">每个选中社区必须在当前客户端填写自己的完整登录账号，共用本机统一密码；账号不会根据组织代码自动拼接。这里不保存居住证会话令牌，远端同步也不会返回账号或密码。</div>
              <div className="grid gap-2 rounded-xl border border-[var(--app-border)] bg-[var(--app-surface-muted)] p-4">
                <div className="text-sm text-[var(--app-text-secondary)]">MAC 地址推送只修改配置的 MAC mock 服务，不修改滨湖平台配置。保存后会立即 GET 回读校验；生产服务应启用写入令牌。</div>
                {macMessage && <Alert type={macMessageType} showIcon message={macMessage} />}
                <div className="flex flex-wrap justify-end gap-2">
                  <Button onClick={() => void readCurrentMac()} loading={macBusy}>读取当前 MAC</Button>
                  <Button type="primary" onClick={() => void pushConfiguredMac()} loading={macBusy} disabled={!config.mac_address.trim()}>保存并推送 MAC</Button>
                </div>
              </div>
              <div className="flex justify-end"><Button type="primary" onClick={persistConfig}>保存离线配置</Button></div>
            </div>
          </Panel>

          <Panel title="已撤管人员居住登记情况批量查询" description="支持 .xlsx。只读取身份证号列，不检查身份证号格式；原表会在身份证号后新增“登记情况”列。">
            <div className="grid gap-4">
              {error && <Alert type="error" showIcon message={error} closable onClose={() => setError('')} />}
              <Dragger accept=".xlsx" maxCount={1} fileList={fileList} beforeUpload={beforeUpload} onRemove={() => { setFile(null); setFileList([]); setWorkbook(null); setStatuses([]); setQueryState('idle') }} disabled={running}><p className="ant-upload-drag-icon"><InboxOutlined /></p><p className="ant-upload-text">拖入人员名单文件，或点击选择</p><p className="ant-upload-hint">识别“身份证号 / 身份证号码 / 身份证”列；不校验号码格式</p></Dragger>
              <div className="flex flex-wrap items-center justify-between gap-3"><span className="text-sm text-[var(--app-text-secondary)]">{file ? `已选择：${file.name}` : '请选择文件后确认查询'}</span><Button type="primary" onClick={() => void start()} loading={running} disabled={!file || running || !configIsUsable(config)}>确认并开始查询</Button></div>
              {workbook && <div className="grid gap-3 rounded-xl border border-[var(--app-border)] bg-[var(--app-surface-muted)] p-4"><Progress percent={total ? Math.round(completed / total * 100) : 0} status={queryState === 'failed' ? 'exception' : queryState === 'completed' ? 'success' : queryState === 'partial' ? 'exception' : undefined} format={() => `${completed}/${total}`} /><div className="flex flex-wrap justify-between gap-2 text-sm"><span>{queryState === 'running' ? '正在直接查询居住证系统' : queryState === 'completed' ? '查询完成' : '查询完成，部分记录需要复核'}</span><span>总人数 {total}，查询成功 {successCount}</span></div>{completed === total && <div className="flex justify-end"><Button type="primary" onClick={() => void exportResult()} loading={exporting}>导出结果 XLSX</Button></div>}</div>}
            </div>
          </Panel>
        </div>
      </div>
    </main>
  )
}
