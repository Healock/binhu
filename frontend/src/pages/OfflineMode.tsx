import { Alert, Button, Input, InputNumber, Progress, Switch, Upload, message } from 'antd'
import { ArrowLeftOutlined, InboxOutlined, ToolOutlined } from '@ant-design/icons'
import type { UploadFile, UploadProps } from 'antd'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Panel } from '../components/ui'
import { getResidencePlatformConfig } from '../api/client'
import { downloadBlob } from '../utils/fileDownload'
import { resolveDesktopBridge } from '../desktop/bridge'
import {
  OfflineResidenceClient,
  cacheOnlineResidenceConfig,
  loadOfflineResidenceConfig,
  normalizeMacAddress,
  normalizeResidenceIdentity,
  readMacAddress,
  saveOfflineResidenceConfig,
  type OfflineResidenceAddressResult,
  type OfflineResidenceConfig,
} from '../utils/offlineResidenceClient'
import { readOfflineWorkbook, writeOfflineWorkbook, type OfflineWorkbook } from '../utils/offlineResidenceXlsx'

const { Dragger } = Upload
type QueryState = 'idle' | 'running' | 'completed' | 'partial' | 'failed'
type BatchQueryMode = 'status' | 'address'

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
  const [registeredAddresses, setRegisteredAddresses] = useState<string[]>([])
  const [queryMode, setQueryMode] = useState<BatchQueryMode>('status')
  const [queryState, setQueryState] = useState<QueryState>('idle')
  const [completed, setCompleted] = useState(0)
  const [successCount, setSuccessCount] = useState(0)
  const [error, setError] = useState('')
  const [configMessage, setConfigMessage] = useState('')
  const [exporting, setExporting] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [macBusy, setMacBusy] = useState(false)
  const [activeMac, setActiveMac] = useState(config.mac_address)
  const [macMessage, setMacMessage] = useState('')
  const [macMessageType, setMacMessageType] = useState<'info' | 'success' | 'error'>('info')
  const [macProbeBusy, setMacProbeBusy] = useState(false)
  const [macProbeProgress, setMacProbeProgress] = useState({ completed: 0, total: 0, mac: '' })
  const [errorCounts, setErrorCounts] = useState<Record<string, number>>({})
  const [diagnosticCounts, setDiagnosticCounts] = useState<Record<string, number>>({})
  const [identityInputSummary, setIdentityInputSummary] = useState<{ empty: number; lengths: Record<string, number>; valid_format: number; invalid_format: number }>({ empty: 0, lengths: {}, valid_format: 0, invalid_format: 0 })
  const macProbeRun = useRef(0)

  const running = queryState === 'running'
  const total = workbook?.rows.length ?? 0
  const configWarning = useMemo(() => {
    if (!config.enabled) return '离线居住证查询已关闭。'
    if (!config.base_url.trim()) return '请填写居住证接口地址。'
    if (!config.password) return '未填写统一登录密码。在线平台不会回传密码，请在此处手动填写。'
    if (!config.accounts.length) return '没有可用的社区账号配置，请先刷新在线范围或在本机填写账号。'
    const incomplete = config.accounts.filter(account => !account.username.trim())
    if (incomplete.length) return `请填写完整居住证登录账号：${incomplete.map(account => account.community_name || '本地账号').join('、')}`
    return ''
  }, [config])

  useEffect(() => {
    const desktop = resolveDesktopBridge()
    if (!desktop) return
    desktop.getLocalMac().then(value => {
      const mac = normalizeMacAddress(value)
      setActiveMac(mac)
      setConfig(current => {
        const next = {
          ...current,
          mac_address: mac,
          mac_addresses: Array.from(new Set([mac, ...current.mac_addresses])),
        }
        saveOfflineResidenceConfig(next)
        return next
      })
    }).catch(() => {})
  }, [])

  const updateConfig = (change: Partial<OfflineResidenceConfig>) => {
    setConfig(current => ({ ...current, ...change }))
    setConfigMessage('')
    setMacMessage('')
  }

  const persistConfig = () => {
    const next = {
      ...config,
      base_url: config.base_url.trim().replace(/\/+$/, ''),
      mac_service_url: 'http://127.0.0.1:23333',
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
      const desktop = resolveDesktopBridge()
      if (!desktop) throw new Error('请使用滨湖 Windows 客户端管理本机 MAC')
      const mac = normalizeMacAddress(await desktop.getLocalMac())
      setActiveMac(mac)
      const next = { ...config, mac_address: mac, mac_addresses: Array.from(new Set([mac, ...config.mac_addresses])) }
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
      const desktop = resolveDesktopBridge()
      if (!desktop) throw new Error('请使用滨湖 Windows 客户端管理本机 MAC')
      const mac = normalizeMacAddress(config.mac_address)
      const saved = normalizeMacAddress(await desktop.setLocalMac(mac))
      const verified = normalizeMacAddress(await desktop.getLocalMac())
      if (saved !== mac || verified !== mac) throw new Error('本机 MAC 保存后回读不一致')
      setActiveMac(verified)
      const next = { ...config, mac_address: verified, mac_addresses: Array.from(new Set([verified, ...config.mac_addresses])) }
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

  const addMacCandidate = () => {
    try {
      const mac = normalizeMacAddress(config.mac_address)
      const next = { ...config, mac_address: mac, mac_addresses: Array.from(new Set([...config.mac_addresses, mac])) }
      saveOfflineResidenceConfig(next)
      setConfig(next)
      setMacMessage(`已加入候选列表：${mac}`)
      setMacMessageType('success')
    } catch (reason) {
      setMacMessage(reason instanceof Error ? reason.message : 'MAC 地址格式无效')
      setMacMessageType('error')
    }
  }

  const activateMacCandidate = async (value: string) => {
    setMacBusy(true)
    setMacMessage('')
    try {
      const desktop = resolveDesktopBridge()
      if (!desktop) throw new Error('请使用滨湖 Windows 客户端管理本机 MAC')
      const mac = normalizeMacAddress(await desktop.setLocalMac(value))
      const verified = normalizeMacAddress(await desktop.getLocalMac())
      if (mac !== verified) throw new Error('本机 MAC 保存后回读不一致')
      setActiveMac(verified)
      const next = { ...config, mac_address: verified, mac_addresses: Array.from(new Set([verified, ...config.mac_addresses])) }
      saveOfflineResidenceConfig(next)
      setConfig(next)
      setMacMessage(`当前使用的 MAC 已切换为：${verified}`)
      setMacMessageType('success')
    } catch (reason) {
      setMacMessage(reason instanceof Error ? reason.message : '切换 MAC 失败')
      setMacMessageType('error')
    } finally {
      setMacBusy(false)
    }
  }

  const removeMacCandidate = (value: string) => {
    if (value === activeMac) {
      setMacMessage('当前正在使用的 MAC 不能移除，请先切换到其他候选地址')
      setMacMessageType('error')
      return
    }
    const next = {
      ...config,
      mac_addresses: config.mac_addresses.filter(mac => mac !== value),
      mac_probe_results: Object.fromEntries(Object.entries(config.mac_probe_results).filter(([mac]) => mac !== value)),
    }
    saveOfflineResidenceConfig(next)
    setConfig(next)
  }

  const probeMacCandidates = async (targets: string[]) => {
    if (macProbeBusy || !targets.length) return
    const incomplete = config.accounts.filter(account => !account.username.trim())
    if (!config.base_url.trim() || !config.password || !config.accounts.length || incomplete.length) {
      setMacMessage('请先填写接口地址、统一密码和全部社区完整登录账号，再检测 MAC 授权范围')
      setMacMessageType('error')
      return
    }
    const runId = ++macProbeRun.current
    const perMac = Math.min(config.accounts.length, 12)
    setMacProbeBusy(true)
    setMacProbeProgress({ completed: 0, total: targets.length * perMac, mac: targets[0] })
    setMacMessage('')
    let nextConfig = config
    try {
      for (let targetIndex = 0; targetIndex < targets.length; targetIndex += 1) {
        if (macProbeRun.current !== runId) break
        const mac = normalizeMacAddress(targets[targetIndex])
        const client = new OfflineResidenceClient(config)
        const results = await client.probeMacAccess(
          mac,
          completed => setMacProbeProgress({ completed: targetIndex * perMac + completed, total: targets.length * perMac, mac }),
          () => macProbeRun.current === runId,
        )
        if (macProbeRun.current !== runId || results.length !== perMac) break
        nextConfig = {
          ...nextConfig,
          mac_probe_results: {
            ...nextConfig.mac_probe_results,
            [mac]: {
              checked_at: new Date().toISOString(),
              probe_version: 1,
              tested_community_count: perMac,
              rejected_community_count: results.filter(result => result.status === 'rejected').length,
              authorized_communities: results.filter(result => result.allowed).map(result => ({
                community_id: result.community_id,
                community_name: result.community_name,
              })),
              probe_failures: results.filter(result => result.status !== 'allowed' && result.status !== 'rejected').map(result => ({
                community_id: result.community_id,
                community_name: result.community_name,
                status: result.status,
                error_code: result.error_code,
              })),
            },
          },
        }
        saveOfflineResidenceConfig(nextConfig)
        setConfig(nextConfig)
      }
      if (macProbeRun.current === runId) {
        setMacMessage('MAC 授权范围检测完成。结果只保存在当前客户端。')
        setMacMessageType('success')
      }
    } catch (reason) {
      setMacMessage(reason instanceof Error ? reason.message : 'MAC 授权范围检测失败')
      setMacMessageType('error')
    } finally {
      if (macProbeRun.current === runId) setMacProbeBusy(false)
    }
  }

  const stopMacProbe = () => {
    macProbeRun.current += 1
    setMacProbeBusy(false)
    setMacMessage('已停止检测；当前请求结束后不会继续测试其他社区账号。')
    setMacMessageType('info')
  }

  const syncOnlineConfig = async () => {
    setSyncing(true)
    setConfigMessage('')
    try {
      const online = await getResidencePlatformConfig()
      const next = cacheOnlineResidenceConfig(online)
      setConfig(next)
      if (!online.base_url?.trim()) {
        setConfigMessage('在线配置尚不完整，已保留当前客户端的离线配置。')
      } else setConfigMessage(online.password_configured && next.accounts.some(account => account.username)
        ? '已同步接口、超时、选中社区范围和社区完整登录账号；统一密码与本机 MAC 仍只保存在当前客户端。'
        : '已同步接口、超时和选中社区范围；请在当前客户端填写尚未配置的完整账号和统一密码。')
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
    setRegisteredAddresses([])
    setCompleted(0)
    setSuccessCount(0)
    setErrorCounts({})
    setDiagnosticCounts({})
    setIdentityInputSummary({ empty: 0, lengths: {}, valid_format: 0, invalid_format: 0 })
    setQueryState('idle')
    setError('')
    return false
  }

  const start = async (mode: BatchQueryMode = 'status') => {
    if (!file) return
    setQueryMode(mode)
    setError('')
    setQueryState('running')
    setCompleted(0)
    setSuccessCount(0)
    setDiagnosticCounts({})
    setIdentityInputSummary({ empty: 0, lengths: {}, valid_format: 0, invalid_format: 0 })
    try {
      const book = await readOfflineWorkbook(file)
      if (!book.rows.length) throw new Error('文件中没有可处理的数据行')
      setWorkbook(book)
      const nextStatuses = Array.from({ length: book.rows.length }, () => '查询中')
      setStatuses(nextStatuses)
      setRegisteredAddresses(Array.from({ length: book.rows.length }, () => ''))
      const client = new OfflineResidenceClient(config)
      let completedCount = 0
      let successfulCount = 0
      const nextErrorCounts: Record<string, number> = {}
      const nextDiagnosticCounts: Record<string, number> = {}
      const identitySummary = { empty: 0, lengths: {} as Record<string, number>, valid_format: 0, invalid_format: 0 }
      const results = [...nextStatuses]
      const concurrency = Math.min(4, Math.max(1, book.rows.length))
      let cursor = 0
      const worker = async () => {
        while (cursor < book.rows.length) {
          const index = cursor
          cursor += 1
          const rawIdentity = String(book.rows[index]?.[book.identityColumn] ?? '').trim().replace(/^[\u0027\u2019]/, '')
          const identity = normalizeResidenceIdentity(rawIdentity)
          if (!rawIdentity) identitySummary.empty += 1
          const lengthKey = String(rawIdentity.length)
          identitySummary.lengths[lengthKey] = (identitySummary.lengths[lengthKey] || 0) + 1
          if (identity) identitySummary.valid_format += 1
          else identitySummary.invalid_format += 1
          setIdentityInputSummary({ ...identitySummary, lengths: { ...identitySummary.lengths } })
          let result: OfflineResidenceAddressResult
          try {
            result = identity
              ? mode === 'address' ? await client.lookupRegistrationAddress(identity) : await client.lookup(identity)
              : { status: '身份证号格式无效', error: 'invalid_identity' }
          } catch (reason) {
            result = { status: '查询失败', error: 'request_error' }
          }
          results[index] = result.status
          if (mode === 'address') {
            setRegisteredAddresses(current => {
              const next = [...current]
              next[index] = result.registered_address || ''
              return next
            })
          }
          if (!result.error) successfulCount += 1
          if (result.error) nextErrorCounts[result.error] = (nextErrorCounts[result.error] || 0) + 1
          for (const event of result.diagnostics || []) {
            const key = [event.stage, event.error_code, event.http_status ?? '', event.business_code ?? '', event.result_type ?? '', event.message_category ?? ''].join('|')
            nextDiagnosticCounts[key] = (nextDiagnosticCounts[key] || 0) + 1
          }
          setErrorCounts({ ...nextErrorCounts })
          setDiagnosticCounts({ ...nextDiagnosticCounts })
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
      setError('名单读取或批量查询失败，请确认文件为有效 XLSX 且含身份证号列')
    }
  }

  const exportResult = async () => {
    if (!workbook || completed !== workbook.rows.length) return
    setExporting(true)
    try {
      const blob = queryMode === 'address'
        ? writeOfflineWorkbook(workbook, statuses, { mode: 'address', addresses: registeredAddresses })
        : writeOfflineWorkbook(workbook, statuses)
      const sourceName = file?.name.replace(/\.xlsx$/i, '') || '居住登记查询结果'
      await downloadBlob(blob, `${sourceName}-${queryMode === 'address' ? '登记地址' : '登记情况'}.xlsx`)
    } catch (reason) {
      setError('结果导出失败，请检查源文件格式和查询结果行数')
    } finally {
      setExporting(false)
    }
  }

  const exportDiagnostics = async () => {
    const desktop = resolveDesktopBridge()
    const snapshot = config.mac_probe_results[activeMac]
    const payload = {
      schema_version: 1,
      generated_at: new Date().toISOString(),
      client_runtime: desktop?.target || (desktop ? 'desktop' : 'browser'),
      base_origin: (() => { try { return new URL(config.base_url).origin } catch { return '' } })(),
      base_path: (() => { try { return new URL(config.base_url).pathname.replace(/\/+$/, '') } catch { return '' } })(),
      timeout_seconds: config.timeout_seconds,
      desktop_bridge_available: Boolean(desktop),
      native_residence_bridge_available: Boolean(desktop?.requestResidenceApi),
      password_configured: Boolean(config.password),
      account_count: config.accounts.length,
      accounts: config.accounts.map(account => ({ community_id: account.community_id, community_name: account.community_name, username_configured: Boolean(account.username.trim()), community_code_configured: Boolean(account.community_code.trim()) })),
      active_mac_masked: activeMac ? `**:**:**:${activeMac.slice(-8)}` : '',
      probe_results: snapshot ? { tested_count: snapshot.tested_community_count, allowed_communities: snapshot.authorized_communities.map(item => item.community_name), rejected_count: snapshot.rejected_community_count || 0, failure_codes: (snapshot.probe_failures || []).reduce<Record<string, number>>((counts, item) => { const code = item.error_code || item.status; counts[code] = (counts[code] || 0) + 1; return counts }, {}) } : null,
      last_batch: { state: queryState, total, completed, success_count: successCount, error_counts: errorCounts, diagnostic_counts: diagnosticCounts, identity_input_summary: identityInputSummary },
    }
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' })
    await downloadBlob(blob, `滨湖离线居住证诊断-${new Date().toISOString().replace(/[-:TZ.]/g, '').slice(0, 14)}.json`)
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

          <Panel title="居住证系统配置" description="配置保存在当前客户端。在线平台同步接口、查询范围和超时；账号、统一密码、本机 MAC 和会话不会上传到平台。" extra={<Button onClick={() => void syncOnlineConfig()} loading={syncing}>刷新在线配置</Button>}>
            <div className="grid gap-4">
              {configWarning && <Alert type="warning" showIcon message={configWarning} />}
              {configMessage && <Alert type="info" showIcon message={configMessage} />}
              <div className="grid gap-4 md:grid-cols-2">
                <label className="settings-field text-sm text-[var(--app-text-strong)]"><span className="settings-field__label font-medium">离线查询开关</span><div className="flex min-h-9 items-center gap-3"><Switch checked={config.enabled} onChange={enabled => updateConfig({ enabled })} /><span>{config.enabled ? '已开启' : '已关闭'}</span></div></label>
                <label className="settings-field text-sm text-[var(--app-text-strong)]"><span className="settings-field__label font-medium">居住证接口地址</span><Input value={config.base_url} onChange={event => updateConfig({ base_url: event.target.value })} /></label>
                <label className="settings-field text-sm text-[var(--app-text-strong)]"><span className="settings-field__label font-medium">统一登录密码</span><Input.Password value={config.password} onChange={event => updateConfig({ password: event.target.value })} autoComplete="new-password" /></label>
                <label className="settings-field text-sm text-[var(--app-text-strong)]"><span className="settings-field__label font-medium">本机 MAC 服务</span><Input value="http://127.0.0.1:23333" readOnly /></label>
                <label className="settings-field text-sm text-[var(--app-text-strong)]"><span className="settings-field__label font-medium">要使用或加入列表的 MAC</span><Input value={config.mac_address} onChange={event => updateConfig({ mac_address: event.target.value })} placeholder="AA:BB:CC:DD:EE:FF" autoComplete="off" /></label>
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
              <div className="text-xs text-[var(--app-text-secondary)]">每个选中社区使用社区管理中配置的完整登录账号，共用本机统一密码；账号不会根据组织代码自动拼接。这里不保存居住证会话令牌，远端同步不会返回密码。</div>
              <div className="grid gap-3 rounded-xl border border-[var(--app-border)] bg-[var(--app-surface-muted)] p-4">
                <div className="text-sm text-[var(--app-text-secondary)]">Windows 客户端首次运行时读取本机硬件 MAC 作为默认值，并在回环地址提供 `23333` 兼容读取端口。候选列表、检测结果和修改都只保存在当前电脑，不会修改服务器 MAC，也不会上传到滨湖平台。</div>
                {macMessage && <Alert type={macMessageType} showIcon message={macMessage} />}
                <div className="flex flex-wrap justify-end gap-2">
                  <Button onClick={() => void readCurrentMac()} loading={macBusy}>读取当前 MAC</Button>
                  <Button onClick={addMacCandidate} disabled={!config.mac_address.trim() || macBusy || macProbeBusy}>加入候选列表</Button>
                  <Button type="primary" onClick={() => void pushConfiguredMac()} loading={macBusy} disabled={!config.mac_address.trim()}>保存本机 MAC</Button>
                </div>
                <div className="grid gap-2">
                  {config.mac_addresses.length === 0 && <div className="text-sm text-[var(--app-text-secondary)]">尚无候选 MAC。Windows 客户端会自动读取一次本机硬件地址，也可以在上方手动添加。</div>}
                  {config.mac_addresses.map(mac => {
                    const snapshot = config.mac_probe_results[mac]
                    const authorized = snapshot?.authorized_communities || []
                    const failures = snapshot?.probe_failures || []
                    return <div key={mac} className="grid gap-2 rounded-lg border border-[var(--app-border)] bg-[var(--app-surface)] p-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-center">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2 text-sm font-medium text-[var(--app-text-strong)]"><span>{mac}</span>{mac === activeMac && <span className="rounded bg-blue-50 px-2 py-0.5 text-xs text-blue-700">当前使用</span>}</div>
                        <div className="mt-1 text-xs text-[var(--app-text-secondary)]">
                          {snapshot && snapshot.probe_version !== 1
                            ? '这是旧版本探测结果，无法区分未授权和网络失败，请重新检测'
                            : snapshot
                            ? failures.length
                              ? `已检测 ${snapshot.tested_community_count} 个社区账号；允许访问：${authorized.length ? authorized.map(item => item.community_name).join('、') : '无法判定'}；${failures.length} 个账号探测失败（网络、证书或配置异常），请先排查桌面客户端网络权限；检测时间：${new Date(snapshot.checked_at).toLocaleString()}`
                              : `已检测 ${snapshot.tested_community_count} 个社区账号；允许访问：${authorized.length ? authorized.map(item => item.community_name).join('、') : '无'}；未授权：${snapshot.rejected_community_count || 0}；检测时间：${new Date(snapshot.checked_at).toLocaleString()}`
                            : '尚未检测可登录的社区账号'}
                        </div>
                      </div>
                      <div className="flex flex-wrap justify-end gap-2">
                        <Button size="small" onClick={() => void activateMacCandidate(mac)} disabled={mac === activeMac || macBusy || macProbeBusy}>设为当前</Button>
                        <Button size="small" onClick={() => void probeMacCandidates([mac])} disabled={macProbeBusy || macBusy}>检测社区</Button>
                        <Button size="small" danger onClick={() => removeMacCandidate(mac)} disabled={mac === activeMac || macBusy || macProbeBusy}>移除</Button>
                      </div>
                    </div>
                  })}
                </div>
                {macProbeBusy && <Progress percent={macProbeProgress.total ? Math.round(macProbeProgress.completed / macProbeProgress.total * 100) : 0} format={() => `${macProbeProgress.completed}/${macProbeProgress.total}`} />}
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-xs text-[var(--app-text-secondary)]">检测会按顺序使用最多 12 个已配置社区账号登录，不并发，不执行登记、修改、删除或写回。</span>
                  <div className="flex gap-2">
                    {macProbeBusy && <Button danger onClick={stopMacProbe}>停止检测</Button>}
                    <Button onClick={() => void probeMacCandidates(config.mac_addresses)} disabled={!config.mac_addresses.length || macProbeBusy || macBusy}>检测全部候选</Button>
                  </div>
                </div>
              </div>
              <div className="flex flex-wrap justify-end gap-2"><Button onClick={() => void exportDiagnostics()}>导出诊断信息</Button><Button type="primary" onClick={persistConfig}>保存离线配置</Button></div>
            </div>
          </Panel>

          <Panel title="已撤管人员居住登记情况批量查询" description="支持 .xlsx。只读取身份证号列（包括“证件号码”）并校验 15/18 位身份证号；格式无效的行不会请求居住证系统。可查询登记状态，或在下方批量查询登记地址；地址查询只读取居住证平台返回的登记地址，并追加到导出文件最后一列。">
            <div className="grid gap-4">
              {error && <Alert type="error" showIcon message={error} closable onClose={() => setError('')} />}
              <Dragger accept=".xlsx" maxCount={1} fileList={fileList} beforeUpload={beforeUpload} onRemove={() => { setFile(null); setFileList([]); setWorkbook(null); setStatuses([]); setRegisteredAddresses([]); setQueryState('idle') }} disabled={running}><p className="ant-upload-drag-icon"><InboxOutlined /></p><p className="ant-upload-text">拖入人员名单文件，或点击选择</p><p className="ant-upload-hint">识别“身份证号 / 身份证号码 / 证件号码 / 公民身份号码 / 身份证”列；不会把“证件类型”列当作身份证号</p></Dragger>
              <div className="flex flex-wrap items-center justify-between gap-3"><span className="text-sm text-[var(--app-text-secondary)]">{file ? `已选择：${file.name}` : '请选择文件后确认查询'}</span><div className="flex flex-wrap gap-2"><Button type="primary" onClick={() => void start('status')} loading={running && queryMode === 'status'} disabled={!file || running || !configIsUsable(config)}>查询登记情况</Button><Button onClick={() => void start('address')} loading={running && queryMode === 'address'} disabled={!file || running || !configIsUsable(config)}>涉警人员信息登记地址批量查询</Button></div></div>
              {workbook && <div className="grid gap-3 rounded-xl border border-[var(--app-border)] bg-[var(--app-surface-muted)] p-4"><Progress percent={total ? Math.round(completed / total * 100) : 0} status={queryState === 'failed' ? 'exception' : queryState === 'completed' ? 'success' : queryState === 'partial' ? 'exception' : undefined} format={() => `${completed}/${total}`} /><div className="flex flex-wrap justify-between gap-2 text-sm"><span>{queryState === 'running' ? `正在直接查询居住证系统${queryMode === 'address' ? '登记地址' : '登记情况'}` : queryState === 'completed' ? '查询完成' : '查询完成，部分记录需要复核'}</span><span>总人数 {total}，查询成功 {successCount}</span></div>{queryMode === 'address' && <div className="text-xs text-[var(--app-text-secondary)]">已找到登记地址 {registeredAddresses.filter(Boolean).length} 条；未登记、查询失败或上游缺少地址的记录保持空白，请结合失败分类复核。</div>}{Object.keys(errorCounts).length > 0 && <div className="text-xs text-[var(--app-text-secondary)]">失败分类：{Object.entries(errorCounts).map(([code, count]) => `${code} ${count} 条`).join('、')}</div>}{completed === total && <div className="flex justify-end"><Button type="primary" onClick={() => void exportResult()} loading={exporting}>导出{queryMode === 'address' ? '登记地址' : '登记情况'}结果 XLSX</Button></div>}</div>}
            </div>
          </Panel>
        </div>
      </div>
    </main>
  )
}
