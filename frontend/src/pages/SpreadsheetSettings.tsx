import { useState, useEffect } from 'react'
import { getSpreadsheetsConfig, saveSpreadsheetsConfig, getSystemConfig, updateSystemConfig, getReportTypes } from '../api/client'

const FIXED_TYPES = [
  '全链条',
  '出租房屋核查',
  '涉警统计',
  '疑似未注销模型三',
  '疑似返苏',
  '寄递业',
  '群租房核查',
]

export default function SpreadsheetSettings() {
  const [configs, setConfigs] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')

  // 总汇总表类型配置
  const [allTypes, setAllTypes] = useState<string[]>([])
  const [selectedTypes, setSelectedTypes] = useState<string[]>([])
  const [savingTypes, setSavingTypes] = useState(false)
  const [typeMsg, setTypeMsg] = useState('')

  useEffect(() => {
    getSpreadsheetsConfig()
      .then((map) => setConfigs(map))
      .finally(() => setLoading(false))

    // 加载总汇总表类型配置
    Promise.all([getSystemConfig(), getReportTypes()]).then(([config, types]) => {
      const subTypes = types.implemented.filter((t: string) => t !== '总汇总表')
      setAllTypes(subTypes)
      try {
        const saved = JSON.parse(config.summary_types || '[]')
        setSelectedTypes(saved.length > 0 ? saved : subTypes)
      } catch { setSelectedTypes(subTypes) }
    }).catch(() => {})
  }, [])

  const handleSave = async () => {
    setSaving(true)
    setMsg('')
    try {
      await saveSpreadsheetsConfig(configs)
      setMsg('保存成功')
    } catch (e: any) {
      setMsg(e?.response?.data?.detail || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const handleToggleType = (type: string) => {
    setSelectedTypes(prev =>
      prev.includes(type) ? prev.filter(t => t !== type) : [...prev, type]
    )
  }

  const handleSaveTypes = async () => {
    setSavingTypes(true); setTypeMsg('')
    try {
      await updateSystemConfig({ summary_types: JSON.stringify(selectedTypes) })
      setTypeMsg('保存成功')
    } catch { setTypeMsg('保存失败') }
    finally { setSavingTypes(false) }
  }

  return (
    <div className="space-y-6">
      <div className="bg-white rounded-lg shadow p-5">
        <h2 className="text-base font-semibold text-gray-800 mb-1">在线表格配置</h2>
        <p className="text-sm text-gray-500 mb-4">为每种表格类型配置对应的腾讯文档链接，保存后系统自动解析</p>

        {loading ? (
          <p className="text-sm text-gray-400">加载中...</p>
        ) : (
          <div className="space-y-4">
            {FIXED_TYPES.map((type) => (
              <div key={type}>
                <label className="block text-xs font-medium text-gray-500 mb-1">{type}</label>
                <input
                  placeholder="https://docs.qq.com/sheet/DZxxxxx?tab=xxxxx"
                  value={configs[type] || ''}
                  onChange={(e) => setConfigs({ ...configs, [type]: e.target.value })}
                  className="w-full border border-gray-300 rounded px-3 py-1.5 text-sm"
                />
              </div>
            ))}
            <div className="flex items-center gap-3 pt-2">
              <button
                onClick={handleSave}
                disabled={saving}
                className="px-4 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50"
              >
                {saving ? '保存中...' : '保存配置'}
              </button>
              {msg && (
                <p className={`text-sm ${msg.includes('成功') ? 'text-green-600' : 'text-red-500'}`}>
                  {msg}
                </p>
              )}
            </div>
          </div>
        )}
      </div>

      {/* 总汇总表类型配置 */}
      <div className="bg-white rounded-lg shadow p-5">
        <h2 className="text-base font-semibold text-gray-800 mb-1">总汇总表配置</h2>
        <p className="text-sm text-gray-500 mb-4">选择哪些分表类型参与总汇总表的数据合并</p>

        <div className="flex flex-wrap gap-4 mb-4">
          {allTypes.map((t) => (
            <label key={t} className="flex items-center gap-2 cursor-pointer">
              <input type="checkbox" checked={selectedTypes.includes(t)} onChange={() => handleToggleType(t)} className="rounded" />
              <span className="text-sm text-gray-700">{t}</span>
            </label>
          ))}
        </div>

        <button onClick={handleSaveTypes} disabled={savingTypes || selectedTypes.length === 0}
          className="px-4 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50">
          {savingTypes ? '保存中...' : '保存配置'}
        </button>
        {typeMsg && (
          <span className={`text-sm ml-3 ${typeMsg.includes('成功') ? 'text-green-600' : 'text-red-500'}`}>{typeMsg}</span>
        )}
      </div>
    </div>
  )
}
