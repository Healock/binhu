import { useState, useEffect } from 'react'
import { getSystemConfig, updateSystemConfig } from '../api/client'

const TIMEZONES = [
  { value: 'Asia/Shanghai', label: '上海 (UTC+8)' },
  { value: 'Asia/Urumqi', label: '乌鲁木齐 (UTC+6)' },
  { value: 'Asia/Tokyo', label: '东京 (UTC+9)' },
  { value: 'Asia/Singapore', label: '新加坡 (UTC+8)' },
  { value: 'UTC', label: 'UTC (UTC+0)' },
  { value: 'America/New_York', label: '纽约 (UTC-5)' },
  { value: 'Europe/London', label: '伦敦 (UTC+0)' },
]

export default function SystemSettings() {
  const [timezone, setTimezone] = useState('Asia/Shanghai')
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    getSystemConfig().then(c => setTimezone(c.timezone || 'Asia/Shanghai')).catch(() => {})
  }, [])

  const handleSave = async () => {
    setSaving(true); setMsg('')
    try {
      await updateSystemConfig({ timezone })
      setMsg('保存成功，刷新页面后生效')
    } catch { setMsg('保存失败') }
    finally { setSaving(false) }
  }

  return (
    <div className="bg-white rounded-lg shadow p-6">
      <h2 className="text-lg font-semibold text-gray-800 mb-4">系统设置</h2>

      <div className="space-y-4">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">系统时区</label>
          <p className="text-xs text-gray-500 mb-2">数据库存储UTC标准时间，前端按此时区显示</p>
          <select value={timezone} onChange={(e) => setTimezone(e.target.value)}
            className="border border-gray-300 rounded px-3 py-2 text-sm w-full md:w-64">
            {TIMEZONES.map((tz) => (
              <option key={tz.value} value={tz.value}>{tz.label}</option>
            ))}
          </select>
        </div>

        <div className="flex items-center gap-3">
          <button onClick={handleSave} disabled={saving}
            className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50">
            {saving ? '保存中...' : '保存'}
          </button>
          {msg && <span className={`text-sm ${msg.includes('成功') ? 'text-green-600' : 'text-red-500'}`}>{msg}</span>}
        </div>
      </div>
    </div>
  )
}
