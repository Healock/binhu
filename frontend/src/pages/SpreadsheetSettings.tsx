import { useState, useEffect } from 'react'
import { Alert, Button, Checkbox, Input } from 'antd'
import { getSpreadsheetsConfig, saveSpreadsheetsConfig, getSystemConfig, updateSystemConfig, getReportTypes } from '../api/client'
import { LoadingState, Panel } from '../components/ui'

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
  const [loadError, setLoadError] = useState('')

  // 总汇总表类型配置
  const [allTypes, setAllTypes] = useState<string[]>([])
  const [selectedTypes, setSelectedTypes] = useState<string[]>([])
  const [savingTypes, setSavingTypes] = useState(false)
  const [typeMsg, setTypeMsg] = useState('')
  const [typeLoadError, setTypeLoadError] = useState('')

  useEffect(() => {
    getSpreadsheetsConfig()
      .then((map) => setConfigs(map))
      .catch(() => setLoadError('表格配置加载失败，请稍后重试'))
      .finally(() => setLoading(false))

    // 加载总汇总表类型配置
    Promise.all([getSystemConfig(), getReportTypes()]).then(([config, types]) => {
      const subTypes = types.implemented.filter((t: string) => t !== '总汇总表')
      setAllTypes(subTypes)
      try {
        const saved = JSON.parse(config.summary_types || '[]')
        setSelectedTypes(saved.length > 0 ? saved : subTypes)
      } catch { setSelectedTypes(subTypes) }
    }).catch(() => setTypeLoadError('汇总类型加载失败，请稍后重试'))
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
      <Panel
        title="在线表格配置"
        description="为每种业务类型填写对应的腾讯文档链接"
      >
        {loading ? (
          <LoadingState />
        ) : loadError ? (
          <Alert type="error" showIcon message={loadError} />
        ) : (
          <div className="space-y-4">
            {FIXED_TYPES.map((type) => (
              <div key={type}>
                <label className="mb-1.5 block text-sm font-medium text-slate-700">{type}</label>
                <Input
                  placeholder="https://docs.qq.com/sheet/DZxxxxx?tab=xxxxx"
                  value={configs[type] || ''}
                  onChange={event => setConfigs({ ...configs, [type]: event.target.value })}
                />
              </div>
            ))}
            <div className="flex items-center gap-3 pt-2">
              <Button
                type="primary"
                onClick={handleSave}
                loading={saving}
              >
                保存配置
              </Button>
              {msg && <Alert type={msg.includes('成功') ? 'success' : 'error'} showIcon message={msg} />}
            </div>
          </div>
        )}
      </Panel>

      <Panel
        title="总汇总表配置"
        description="选择哪些业务类型参与总汇总表的数据合并"
      >
        {typeLoadError && <Alert className="mb-4" type="error" showIcon message={typeLoadError} />}
        <div className="mb-5 flex flex-wrap gap-4">
          {allTypes.map((t) => (
            <Checkbox key={t} checked={selectedTypes.includes(t)} onChange={() => handleToggleType(t)}>
              {t}
            </Checkbox>
          ))}
        </div>

        <Button
          type="primary"
          onClick={handleSaveTypes}
          loading={savingTypes}
          disabled={selectedTypes.length === 0}
        >
          保存配置
        </Button>
        {typeMsg && <Alert className="mt-4" type={typeMsg.includes('成功') ? 'success' : 'error'} showIcon message={typeMsg} />}
      </Panel>
    </div>
  )
}
