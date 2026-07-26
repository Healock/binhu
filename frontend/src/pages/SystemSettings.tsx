import { useState, useEffect } from 'react'
import { Alert, Button, Select } from 'antd'
import { getSystemConfig, updateSystemConfig } from '../api/client'
import { Panel } from '../components/ui'

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
    getSystemConfig()
      .then(c => setTimezone(c.timezone || 'Asia/Shanghai'))
      .catch(() => setMsg('系统设置加载失败，请稍后重试'))
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
    <Panel title="系统设置" description="设置系统时间在页面上的显示方式">
      <div className="space-y-4">
        <div>
          <label className="mb-1.5 block text-sm font-medium text-slate-700">系统时区</label>
          <p className="mb-2 text-xs text-slate-500">数据库保存 UTC 标准时间，页面按照这里选择的时区显示。</p>
          <Select
            value={timezone}
            onChange={setTimezone}
            className="w-full md:w-72"
            options={TIMEZONES}
          />
        </div>

        <div className="flex items-center gap-3">
          <Button type="primary" onClick={handleSave} loading={saving}>保存</Button>
        </div>
        {msg && <Alert type={msg.includes('成功') ? 'success' : 'error'} showIcon message={msg} />}
      </div>
    </Panel>
  )
}
