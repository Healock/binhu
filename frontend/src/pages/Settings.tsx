import { useState, useEffect } from 'react'
import type { SpreadsheetCreate, OAuthConfig } from '../types'
import { useSpreadsheets } from '../hooks/useSpreadsheets'
import { saveOAuth, testOAuth, getAuthStatus } from '../api/client'

export default function Settings() {
  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <SpreadsheetSection />
      <OAuthSection />
    </div>
  )
}

// ---- 电子表格管理 ----
function SpreadsheetSection() {
  const { spreadsheets, loading, add, remove } = useSpreadsheets()
  const [name, setName] = useState('')
  const [fileId, setFileId] = useState('')
  const [dataSheetId, setDataSheetId] = useState('000001')
  const [submitting, setSubmitting] = useState(false)
  const [msg, setMsg] = useState('')

  const handleAdd = async () => {
    if (!name.trim() || !fileId.trim()) return
    setSubmitting(true)
    setMsg('')
    try {
      await add({ name: name.trim(), file_id: fileId.trim(), data_sheet_id: dataSheetId.trim() || '000001' })
      setName('')
      setFileId('')
      setDataSheetId('000001')
      setMsg('添加成功')
    } catch (e: any) {
      setMsg(e?.response?.data?.detail || '添加失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="bg-white rounded-lg shadow p-5">
      <h2 className="text-base font-semibold text-gray-800 mb-4">电子表格配置</h2>

      <div className="flex gap-3 mb-4 flex-wrap">
        <input
          placeholder="表格名称"
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="border border-gray-300 rounded px-3 py-1.5 text-sm w-40"
        />
        <input
          placeholder="fileId（从URL提取）"
          value={fileId}
          onChange={(e) => setFileId(e.target.value)}
          className="border border-gray-300 rounded px-3 py-1.5 text-sm w-64"
        />
        <input
          placeholder="数据子表ID"
          value={dataSheetId}
          onChange={(e) => setDataSheetId(e.target.value)}
          className="border border-gray-300 rounded px-3 py-1.5 text-sm w-24"
        />
        <button
          onClick={handleAdd}
          disabled={submitting}
          className="px-4 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50"
        >
          {submitting ? '添加中...' : '添加'}
        </button>
      </div>
      {msg && <p className="text-sm text-gray-500 mb-3">{msg}</p>}

      {loading ? (
        <p className="text-sm text-gray-400">加载中...</p>
      ) : spreadsheets.length === 0 ? (
        <p className="text-sm text-gray-400">暂无配置的表格</p>
      ) : (
        <ul className="divide-y divide-gray-100">
          {spreadsheets.map((s) => (
            <li key={s.id} className="flex items-center justify-between py-2.5">
              <div>
                <span className="text-sm font-medium text-gray-800">{s.name}</span>
                <span className="text-xs text-gray-400 ml-2">{s.file_id}</span>
                {!s.enabled && <span className="text-xs text-red-500 ml-2">已禁用</span>}
              </div>
              <button
                onClick={() => remove(s.id)}
                className="text-xs text-red-500 hover:text-red-700"
              >
                删除
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function OAuthSection() {
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
      <h2 className="text-base font-semibold text-gray-800 mb-4">OAuth 认证配置</h2>
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
