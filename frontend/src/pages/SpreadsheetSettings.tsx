import { useState, useEffect } from 'react'
import { Alert, Button, Input } from 'antd'
import {
  getSpreadsheetsConfig,
  saveSpreadsheetsConfig,
} from '../api/client'
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

  useEffect(() => {
    getSpreadsheetsConfig()
      .then((map) => setConfigs(map))
      .catch(() => setLoadError('表格配置加载失败，请稍后重试'))
      .finally(() => setLoading(false))
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

    </div>
  )
}
