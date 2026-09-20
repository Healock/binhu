import { useEffect, useState } from 'react'
import { Alert, Button, Input } from 'antd'
import { getServerMacConfig, updateServerMacConfig } from '../api/client'
import { Panel } from '../components/ui'
import { normalizeMacAddress } from '../utils/offlineResidenceClient'

export default function ServerMacSettings() {
  const [mac, setMac] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const [messageType, setMessageType] = useState<'success' | 'error' | 'info'>('info')

  useEffect(() => {
    getServerMacConfig()
      .then(config => setMac(config.mac))
      .catch(() => {
        setMessageType('error')
        setMessage('服务器 MAC 配置加载失败，请稍后重试')
      })
      .finally(() => setLoading(false))
  }, [])

  const save = async () => {
    setSaving(true)
    setMessage('')
    try {
      const normalized = normalizeMacAddress(mac)
      const result = await updateServerMacConfig(normalized)
      setMac(result.mac)
      setMessageType('success')
      setMessage('服务器 MAC 已更新，旧的居住证登录会话已清除，后续查询会使用新地址重新登录')
    } catch (error: any) {
      setMessageType('error')
      setMessage(error?.response?.data?.detail || error?.message || '服务器 MAC 保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Panel
      title="服务器 MAC 地址"
      description="管理员和超级管理员可调整生产服务器执行居住证只读查询时使用的 MAC。兼容读取端口固定为服务器 23333。"
    >
      <div className="grid gap-4">
        <Alert
          type="info"
          showIcon
          message="服务器与客户端配置相互独立"
          description="这里修改服务器 MAC；普通用户在离线模式中修改的是自己电脑的本机 MAC。两者不会互相覆盖。"
        />
        {message && <Alert type={messageType} showIcon message={message} />}
        <div className="grid gap-4 md:grid-cols-2">
          <label className="settings-field text-sm text-[var(--app-text-strong)]">
            <span className="settings-field__label font-medium">服务器 MAC</span>
            <Input
              value={mac}
              onChange={event => setMac(event.target.value)}
              placeholder="02:11:22:33:44:66"
              disabled={loading || saving}
              autoComplete="off"
            />
            <span className="settings-field__hint text-xs text-[var(--app-text-secondary)]">
              支持冒号、连字符、点号或连续 12 位十六进制输入；系统会统一保存为大写冒号格式。
            </span>
          </label>
          <label className="settings-field text-sm text-[var(--app-text-strong)]">
            <span className="settings-field__label font-medium">兼容读取地址</span>
            <Input value="http://服务器地址:23333/" readOnly />
            <span className="settings-field__hint text-xs text-[var(--app-text-secondary)]">
              该端口只提供读取，不接受匿名写入；修改必须通过当前登录页面完成。
            </span>
          </label>
        </div>
        <div className="settings-actions flex justify-end">
          <Button type="primary" loading={saving} disabled={loading || !mac.trim()} onClick={() => void save()}>
            保存服务器 MAC
          </Button>
        </div>
      </div>
    </Panel>
  )
}
