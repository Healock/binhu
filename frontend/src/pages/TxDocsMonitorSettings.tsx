import { useEffect, useState } from 'react'
import { Alert, Button, Input, InputNumber, Select, Space, Switch } from 'antd'
import { useNavigate } from 'react-router-dom'
import { PageHeader, Panel } from '../components/ui'
import {
  apiErrorMessage,
  disableTxDocsMonitorConfig,
  getTxDocsMonitorConfig,
  runTxDocsMonitorNow,
  updateTxDocsMonitorConfig,
  type TxDocsMonitorConfig,
} from '../api/client'

const PARSER_TYPES = ['全链条', '出租房屋核查', '寄递业', '疑似返苏', '苏州涉警', '交通涉警']

export default function TxDocsMonitorSettings() {
  const navigate = useNavigate()
  const [config, setConfig] = useState<TxDocsMonitorConfig | null>(null)
  const [url, setUrl] = useState('')
  const [sheetId, setSheetId] = useState('')
  const [parserType, setParserType] = useState('全链条')
  const [headerRow, setHeaderRow] = useState(1)
  const [interval, setInterval] = useState(600)
  const [clientId, setClientId] = useState('')
  const [accessToken, setAccessToken] = useState('')
  const [openId, setOpenId] = useState('')
  const [enabled, setEnabled] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [running, setRunning] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const value = await getTxDocsMonitorConfig()
      setConfig(value)
      setUrl(value.spreadsheet_url || '')
      setSheetId(value.data_sheet_id)
      setParserType(value.parser_type || '全链条')
      setHeaderRow(value.header_row || 1)
      setInterval(value.interval_seconds || 600)
      setEnabled(value.enabled)
    } catch (cause: unknown) {
      setError(apiErrorMessage(cause, '配置加载失败，请稍后重试'))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [])

  const save = async () => {
    setSaving(true); setMessage(''); setError('')
    try {
      const value = await updateTxDocsMonitorConfig({
        spreadsheet_url: url,
        data_sheet_id: sheetId,
        parser_type: parserType,
        header_row: headerRow,
        interval_seconds: interval,
        client_id: clientId,
        access_token: accessToken,
        open_id: openId,
        enabled,
      })
      setConfig(value); setAccessToken(''); setMessage('配置已保存。Token 不会在页面回显。')
    } catch (cause: unknown) {
      setError(apiErrorMessage(cause, '保存失败，未修改现有配置'))
    } finally { setSaving(false) }
  }

  const disable = async () => {
    setSaving(true); setError(''); setMessage('')
    try { await disableTxDocsMonitorConfig(); setEnabled(false); setMessage('监控已禁用') } catch { setError('禁用失败，请稍后重试') } finally { setSaving(false) }
  }

  const runNow = async () => {
    setRunning(true); setError(''); setMessage('')
    try { const result = await runTxDocsMonitorNow(); setMessage(result.message) } catch (cause: unknown) { setError(apiErrorMessage(cause, '读取失败，请查看监控状态')) } finally { setRunning(false) }
  }

  return (
    <div className="grid gap-4">
      <PageHeader title="腾讯只读监控配置" description="仅用于外部腾讯表的只读监控与汇总，不导入平台任务、不回写或删除腾讯数据。" actions={<Button onClick={() => navigate('/summary')}>返回在线数据汇总</Button>} />
      {error && <Alert type="error" showIcon message={error} />}
      {message && <Alert type="success" showIcon message={message} />}
      <Panel title="监控目标" description="仅支持腾讯文档表格链接；监控器只读取聚合统计。">
        <div className="grid gap-3 md:grid-cols-2">
          <label className="settings-field"><span className="settings-field__label">腾讯表链接</span><Input placeholder="https://docs.qq.com/sheet/..." value={url} onChange={event => setUrl(event.target.value)} /></label>
          <label className="settings-field"><span className="settings-field__label">数据子表 ID</span><Input value={sheetId} onChange={event => setSheetId(event.target.value)} /></label>
          <label className="settings-field"><span className="settings-field__label">解析业务类型</span><Select className="w-full" value={parserType} onChange={setParserType} options={PARSER_TYPES.map(value => ({ value, label: value }))} /></label>
          <label className="settings-field"><span className="settings-field__label">表头行号</span><InputNumber className="w-full" min={1} max={100} value={headerRow} onChange={value => setHeaderRow(Number(value || 1))} /></label>
          <label className="settings-field"><span className="settings-field__label">自动读取间隔（秒）</span><InputNumber className="w-full" min={60} max={86400} value={interval} onChange={value => setInterval(Number(value || 600))} /></label>
          <label className="settings-field flex items-center justify-between"><span className="settings-field__label">启用只读监控</span><Switch checked={enabled} onChange={setEnabled} /></label>
        </div>
      </Panel>
      <Panel title="只读凭据" description="填写新 Token 时会加密保存；旧 Token 只显示为已配置，不会回显。">
        <div className="grid gap-3 md:grid-cols-2">
          <label className="settings-field"><span className="settings-field__label">Client ID</span><Input value={clientId} onChange={event => setClientId(event.target.value)} placeholder={config?.client_id_configured ? '已配置，留空表示保持不变' : ''} /></label>
          <label className="settings-field"><span className="settings-field__label">Access Token</span><Input.Password value={accessToken} onChange={event => setAccessToken(event.target.value)} placeholder={config?.access_token_configured ? '已配置，留空表示保持不变' : ''} /></label>
          <label className="settings-field"><span className="settings-field__label">Open ID</span><Input value={openId} onChange={event => setOpenId(event.target.value)} placeholder={config?.open_id_configured ? '已配置，留空表示保持不变' : ''} /></label>
        </div>
      </Panel>
      <Panel title="操作" description={loading ? '正在读取配置…' : `当前状态：${config?.status || '未配置'}`}>
        <Space wrap>
          <Button type="primary" loading={saving} onClick={() => void save()}>保存配置</Button>
          <Button loading={running} disabled={!enabled || saving} onClick={() => void runNow()}>立即读取一次</Button>
          <Button danger loading={saving} disabled={!config?.enabled} onClick={() => void disable()}>禁用监控</Button>
        </Space>
      </Panel>
    </div>
  )
}
