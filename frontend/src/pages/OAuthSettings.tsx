import { useState, useEffect } from 'react'
import { Alert, Button, Input } from 'antd'
import { SafetyCertificateOutlined } from '@ant-design/icons'
import type { OAuthConfig } from '../types'
import { saveOAuth, testOAuth, getAuthStatus } from '../api/client'
import { Panel } from '../components/ui'

export default function OAuthSettings() {
  const [clientId, setClientId] = useState('')
  const [clientSecret, setClientSecret] = useState('')
  const [accessToken, setAccessToken] = useState('')
  const [refreshToken, setRefreshToken] = useState('')
  const [openId, setOpenId] = useState('')
  const [statusMsg, setStatusMsg] = useState('')
  const [loadError, setLoadError] = useState('')
  const [testing, setTesting] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    getAuthStatus().then((s) => {
      if (s.configured) {
        setClientId(s.client_id)
        setOpenId(s.open_id)
      }
    }).catch(() => setLoadError('OAuth 配置状态加载失败，请稍后重试'))
  }, [])

  const handleSave = async () => {
    setSaving(true)
    setStatusMsg('')
    try {
      await saveOAuth({
        client_id: clientId,
        client_secret: clientSecret,
        access_token: accessToken,
        refresh_token: refreshToken,
        open_id: openId,
      })
      setStatusMsg('保存成功')
    } catch (e: any) {
      setStatusMsg(e?.response?.data?.detail || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async () => {
    setTesting(true)
    setStatusMsg('')
    try {
      const r = await testOAuth({
        client_id: clientId,
        client_secret: clientSecret,
        access_token: accessToken,
        refresh_token: refreshToken,
        open_id: openId,
      })
      setStatusMsg(r.valid ? '测试成功：凭据有效' : `测试失败：${r.message}`)
    } catch (e: any) {
      setStatusMsg(`测试失败：${e?.message || '未知错误'}`)
    } finally {
      setTesting(false)
    }
  }

  return (
    <Panel
      title="腾讯文档 OAuth 认证"
      description="用于读取和写入已授权的腾讯文档"
    >
      <div className="flex flex-col gap-5">
        <Alert
          type="warning"
          showIcon
          icon={<SafetyCertificateOutlined />}
          message="这些字段属于敏感信息，请勿复制到聊天、截图或共享文档。"
        />
        {loadError && <Alert type="error" showIcon message={loadError} />}
        <div className="flex flex-col gap-4">
          <div>
            <label className="mb-1.5 block text-sm font-medium text-slate-700">Client-Id</label>
            <Input
              value={clientId}
              onChange={event => setClientId(event.target.value)}
            />
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium text-slate-700">Client-Secret</label>
            <Input.Password
              value={clientSecret}
              onChange={event => setClientSecret(event.target.value)}
            />
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium text-slate-700">Access-Token</label>
            <Input.Password
              value={accessToken}
              onChange={event => setAccessToken(event.target.value)}
            />
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium text-slate-700">Refresh-Token（可选）</label>
            <Input.Password
              value={refreshToken}
              onChange={event => setRefreshToken(event.target.value)}
            />
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium text-slate-700">Open-Id</label>
            <Input
              value={openId}
              onChange={event => setOpenId(event.target.value)}
            />
          </div>
          <div className="flex justify-end gap-3 border-t border-slate-200 pt-4">
            <Button
              onClick={handleTest}
              loading={testing}
            >
              测试连接
            </Button>
            <Button
              type="primary"
              onClick={handleSave}
              loading={saving}
            >
              保存
            </Button>
          </div>
          {statusMsg && (
            <Alert type={statusMsg.includes('成功') ? 'success' : 'error'} showIcon message={statusMsg} />
          )}
        </div>
      </div>
    </Panel>
  )
}
