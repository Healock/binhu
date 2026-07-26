import { useState, useEffect } from 'react'
import type { OAuthConfig } from '../types'
import { saveOAuth, testOAuth, getAuthStatus } from '../api/client'

export default function OAuthSettings() {
  const [clientId, setClientId] = useState('')
  const [clientSecret, setClientSecret] = useState('')
  const [accessToken, setAccessToken] = useState('')
  const [refreshToken, setRefreshToken] = useState('')
  const [openId, setOpenId] = useState('')
  const [statusMsg, setStatusMsg] = useState('')
  const [testing, setTesting] = useState(false)

  useEffect(() => {
    getAuthStatus().then((s) => {
      if (s.configured) {
        setClientId(s.client_id)
        setOpenId(s.open_id)
      }
    }).catch(() => {})
  }, [])

  const handleSave = async () => {
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
    <div className="bg-white rounded-lg shadow p-5">
      <h2 className="text-base font-semibold text-gray-800 mb-4">腾讯文档 OAuth 认证</h2>
      <div className="space-y-3">
        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1">Client-Id</label>
          <input
            value={clientId}
            onChange={(e) => setClientId(e.target.value)}
            className="w-full border border-gray-300 rounded px-3 py-1.5 text-sm"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1">Client-Secret</label>
          <input
            type="password"
            value={clientSecret}
            onChange={(e) => setClientSecret(e.target.value)}
            className="w-full border border-gray-300 rounded px-3 py-1.5 text-sm"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1">Access-Token</label>
          <input
            type="password"
            value={accessToken}
            onChange={(e) => setAccessToken(e.target.value)}
            className="w-full border border-gray-300 rounded px-3 py-1.5 text-sm"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1">Refresh-Token（可选）</label>
          <input
            type="password"
            value={refreshToken}
            onChange={(e) => setRefreshToken(e.target.value)}
            className="w-full border border-gray-300 rounded px-3 py-1.5 text-sm"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1">Open-Id</label>
          <input
            value={openId}
            onChange={(e) => setOpenId(e.target.value)}
            className="w-full border border-gray-300 rounded px-3 py-1.5 text-sm"
          />
        </div>
        <div className="flex gap-3 pt-2">
          <button
            onClick={handleTest}
            disabled={testing}
            className="px-4 py-1.5 border border-gray-300 rounded text-sm hover:bg-gray-50 disabled:opacity-50"
          >
            {testing ? '测试中...' : '测试连接'}
          </button>
          <button
            onClick={handleSave}
            className="px-4 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700"
          >
            保存
          </button>
        </div>
        {statusMsg && (
          <p className={`text-sm ${statusMsg.includes('成功') ? 'text-green-600' : 'text-red-500'}`}>
            {statusMsg}
          </p>
        )}
      </div>
    </div>
  )
}
