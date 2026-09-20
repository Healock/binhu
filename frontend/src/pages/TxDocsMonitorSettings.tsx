import { useEffect, useState } from 'react'
import { Alert, Button, Input, InputNumber, Popconfirm, Select, Space, Switch } from 'antd'
import { useNavigate } from 'react-router-dom'
import { PageHeader, Panel } from '../components/ui'
import {
  apiErrorMessage,
  deleteTxDocsMonitorConfig,
  disableTxDocsMonitorConfig,
  getTxDocsMonitorConfig,
  runTxDocsMonitorNow,
  updateTxDocsMonitorConfig,
  updateTxDocsMonitorCredentials,
  type TxDocsMonitorConfig,
  type TxDocsMonitorTarget,
} from '../api/client'

const PARSER_TYPES = ['全链条', '出租房屋核查', '寄递业', '疑似未注销模型三', '疑似返苏', '苏州涉警', '交通涉警']

const newTarget = (): TxDocsMonitorTarget => ({
  id: -Date.now(),
  enabled: false,
  configured: false,
  spreadsheet_url_configured: false,
  spreadsheet_url: '',
  file_id: '',
  data_sheet_id: '',
  header_row: 1,
  parser_type: '全链条',
  interval_seconds: 600,
  status: '未保存',
  updated_at: null,
})

function tabFromUrl(value: string): string {
  try {
    return new URL(value).searchParams.get('tab')?.trim() || ''
  } catch {
    return ''
  }
}

export default function TxDocsMonitorSettings() {
  const navigate = useNavigate()
  const [config, setConfig] = useState<TxDocsMonitorConfig | null>(null)
  const [targets, setTargets] = useState<TxDocsMonitorTarget[]>([])
  const [clientId, setClientId] = useState('')
  const [accessToken, setAccessToken] = useState('')
  const [openId, setOpenId] = useState('')
  const [loading, setLoading] = useState(true)
  const [savingId, setSavingId] = useState<number | null>(null)
  const [savingCredentials, setSavingCredentials] = useState(false)
  const [running, setRunning] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const value = await getTxDocsMonitorConfig()
      setConfig(value)
      setTargets(value.targets || [])
    } catch (cause: unknown) {
      setError(apiErrorMessage(cause, '配置加载失败，请稍后重试'))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [])

  const updateTarget = (id: number, patch: Partial<TxDocsMonitorTarget>) => {
    setTargets(current => current.map(target => target.id === id ? { ...target, ...patch } : target))
  }

  const saveTarget = async (target: TxDocsMonitorTarget) => {
    setSavingId(target.id); setMessage(''); setError('')
    try {
      const value = await updateTxDocsMonitorConfig({
        target_id: target.id > 0 ? target.id : undefined,
        spreadsheet_url: target.spreadsheet_url,
        data_sheet_id: target.data_sheet_id,
        parser_type: target.parser_type,
        header_row: target.header_row,
        interval_seconds: target.interval_seconds,
        enabled: target.enabled,
      })
      setConfig(value)
      setTargets(value.targets || [])
      setMessage('监控目标已保存（共 ' + (value.targets?.length || 0) + ' 个目标）。')
    } catch (cause: unknown) {
      setError(apiErrorMessage(cause, '保存失败，未修改现有配置'))
    } finally {
      setSavingId(null)
    }
  }

  const saveCredentials = async () => {
    setSavingCredentials(true); setMessage(''); setError('')
    try {
      const value = await updateTxDocsMonitorCredentials({
        client_id: clientId.trim(),
        access_token: accessToken.trim(),
        open_id: openId.trim(),
      })
      setConfig(value)
      setTargets(value.targets || [])
      setClientId('')
      setAccessToken('')
      setOpenId('')
      setMessage('只读连接凭据已保存。现在可以立即读取；凭据不会回显。')
    } catch (cause: unknown) {
      setError(apiErrorMessage(cause, '凭据保存失败，未修改现有连接'))
    } finally {
      setSavingCredentials(false)
    }
  }

  const removeTarget = async (target: TxDocsMonitorTarget) => {
    setSavingId(target.id); setMessage(''); setError('')
    try {
      if (target.id > 0) await deleteTxDocsMonitorConfig(target.id)
      setTargets(current => current.filter(item => item.id !== target.id))
      setMessage('监控目标已移除；历史只读统计快照仍保留。')
    } catch (cause: unknown) {
      setError(apiErrorMessage(cause, '移除失败，请稍后重试'))
    } finally {
      setSavingId(null)
    }
  }

  const disable = async () => {
    setSavingId(0); setError(''); setMessage('')
    try { await disableTxDocsMonitorConfig(); await load(); setMessage('所有只读监控目标已禁用') }
    catch (cause: unknown) { setError(apiErrorMessage(cause, '禁用失败，请稍后重试')) }
    finally { setSavingId(null) }
  }

  const runNow = async () => {
    setRunning(true); setError(''); setMessage('')
    try { const result = await runTxDocsMonitorNow(); setMessage(result.message) }
    catch (cause: unknown) { setError(apiErrorMessage(cause, '读取失败，请查看监控状态')) }
    finally { setRunning(false) }
  }

  return (
    <div className="grid gap-4">
      <PageHeader
        title="腾讯只读监控配置"
        description="仅用于外部腾讯表的只读监控与汇总，不导入平台任务、不回写或删除腾讯数据。一个只读连接可以配置多个业务表和子表。"
        actions={<Button onClick={() => navigate('/summary')}>返回在线数据汇总</Button>}
      />
      {!config?.environment_allowed && !loading && (
        <Alert
          type="warning"
          showIcon
          message="当前环境不允许启用腾讯只读监控"
          description="腾讯只读监控只允许在 Production 环境使用；Development、Staging 和 Shadow 由后端硬性禁止。Production 中启用或禁用目标后立即生效，无需修改环境变量或重启 backend。"
        />
      )}
      {error && <Alert type="error" showIcon message={error} />}
      {message && <Alert type="success" showIcon message={message} />}

      <Panel title="只读连接凭据" description="凭据在服务器加密保存并由所有监控目标共享；Token 不会回显，也不会进入监控快照。">
        <div className="grid gap-3">
          <Alert type="info" showIcon message="三个字段必须来自同一次腾讯授权。修改时请完整填写并单独保存；保存后不会回显。" />
          <div className="grid gap-3 md:grid-cols-2">
            <label className="settings-field"><span className="settings-field__label">Client ID</span><Input value={clientId} onChange={event => setClientId(event.target.value)} placeholder={config?.client_id_configured ? '已配置；更新时重新完整填写' : ''} /></label>
            <label className="settings-field"><span className="settings-field__label">Access Token</span><Input.Password value={accessToken} onChange={event => setAccessToken(event.target.value)} placeholder={config?.access_token_configured ? '已配置；更新时重新完整填写' : ''} /></label>
            <label className="settings-field"><span className="settings-field__label">Open ID</span><Input value={openId} onChange={event => setOpenId(event.target.value)} placeholder={config?.open_id_configured ? '已配置；更新时重新完整填写' : ''} /></label>
          </div>
          <div>
            <Button
              type="primary"
              loading={savingCredentials}
              disabled={!config?.environment_allowed || !clientId.trim() || !accessToken.trim() || !openId.trim() || savingId !== null}
              onClick={() => void saveCredentials()}
            >
              保存连接凭据
            </Button>
          </div>
        </div>
      </Panel>

      <Panel title="监控目标" description="每个目标对应一个业务解析类型和腾讯子表；同一个 docs.qq.com/sheet 文件可以通过不同 tab 配置多个目标。Production 中保存后立即生效，无需修改环境变量或重启 backend。">
        <div className="grid gap-4">
          {targets.map(target => (
            <div key={target.id} className="grid gap-3 rounded-lg border border-[var(--app-border)] p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <strong>目标 {target.id > 0 ? '#' + target.id : '（新目标）'}</strong>
                <Space wrap>
                  <span className="text-sm text-[var(--app-text-muted)]">{target.status}</span>
                  <Switch checked={target.enabled} disabled={!config?.environment_allowed} onChange={enabled => updateTarget(target.id, { enabled })} checkedChildren="启用" unCheckedChildren="停用" />
                  <Button type="primary" loading={savingId === target.id} disabled={savingCredentials} onClick={() => void saveTarget(target)}>保存目标</Button>
                  <Popconfirm title="移除此监控目标？" description="历史快照不会删除。" onConfirm={() => void removeTarget(target)} okText="移除" cancelText="取消">
                    <Button danger loading={savingId === target.id}>移除</Button>
                  </Popconfirm>
                </Space>
              </div>
              <div className="grid gap-3 md:grid-cols-2">
                <label className="settings-field md:col-span-2"><span className="settings-field__label">腾讯表链接</span><Input placeholder="https://docs.qq.com/sheet/DRV...?...tab=BB08J2" value={target.spreadsheet_url} onChange={event => { const value = event.target.value; const tab = tabFromUrl(value); updateTarget(target.id, { spreadsheet_url: value, data_sheet_id: tab || target.data_sheet_id }) }} /></label>
                <label className="settings-field"><span className="settings-field__label">数据子表 ID（tab）</span><Input value={target.data_sheet_id} onChange={event => updateTarget(target.id, { data_sheet_id: event.target.value })} /></label>
                <label className="settings-field"><span className="settings-field__label">解析业务类型</span><Select className="w-full" value={target.parser_type} onChange={parser_type => updateTarget(target.id, { parser_type })} options={PARSER_TYPES.map(value => ({ value, label: value }))} /></label>
                <label className="settings-field"><span className="settings-field__label">表头行号</span><InputNumber className="w-full" min={1} max={100} value={target.header_row} onChange={value => updateTarget(target.id, { header_row: Number(value || 1) })} /></label>
                <label className="settings-field"><span className="settings-field__label">自动读取间隔（秒）</span><InputNumber className="w-full" min={60} max={86400} value={target.interval_seconds} onChange={value => updateTarget(target.id, { interval_seconds: Number(value || 600) })} /></label>
              </div>
            </div>
          ))}
          {!targets.length && <div className="text-sm text-[var(--app-text-muted)]">尚未配置监控目标，请先添加一个业务表。</div>}
          <Space wrap>
            <Button onClick={() => setTargets(current => [...current, newTarget()])}>新增监控目标</Button>
            <Button loading={running} disabled={!config?.environment_allowed || !config?.configured || !targets.some(target => target.enabled) || savingId !== null || savingCredentials} onClick={() => void runNow()}>立即读取一次</Button>
            <Button danger loading={savingId === 0} disabled={!targets.some(target => target.enabled) || savingId !== null} onClick={() => void disable()}>禁用全部监控</Button>
          </Space>
        </div>
      </Panel>
    </div>
  )
}
