import { useState, useEffect } from 'react'

export default function PersonalizationSettings() {
  const [displayMode, setDisplayMode] = useState<'table' | 'card'>('table')
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    const saved = (localStorage.getItem('table_display_mode') as 'table' | 'card') || 'table'
    setDisplayMode(saved)
  }, [])

  const handleSave = async () => {
    setSaving(true); setMsg('')
    try {
      localStorage.setItem('table_display_mode', displayMode)
      setMsg('保存成功，刷新页面后生效')
    } catch { setMsg('保存失败') }
    finally { setSaving(false) }
  }

  return (
    <div className="bg-white rounded-lg shadow p-6">
      <h2 className="text-lg font-semibold text-gray-800 mb-2">个性化</h2>
      <p className="text-sm text-gray-500 mb-4">选择数据列表的显示方式（移动端建议使用卡片模式）</p>

      <div className="flex gap-4 mb-4">
        <label className="flex items-center gap-2 cursor-pointer">
          <input type="radio" name="displayMode" value="table"
            checked={displayMode === 'table'}
            onChange={() => setDisplayMode('table')} />
          <span className="text-sm text-gray-700">表格模式</span>
        </label>
        <label className="flex items-center gap-2 cursor-pointer">
          <input type="radio" name="displayMode" value="card"
            checked={displayMode === 'card'}
            onChange={() => setDisplayMode('card')} />
          <span className="text-sm text-gray-700">卡片模式</span>
        </label>
      </div>

      <button onClick={handleSave} disabled={saving}
        className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50">
        {saving ? '保存中...' : '保存'}
      </button>
      {msg && <span className={`text-sm ml-3 ${msg.includes('成功') ? 'text-green-600' : 'text-red-500'}`}>{msg}</span>}
    </div>
  )
}
