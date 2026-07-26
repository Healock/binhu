import { useState, useEffect } from 'react'
import { Alert, Button, Radio } from 'antd'
import { Panel } from '../components/ui'

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
    <Panel
      title="个性化"
      description="选择数据列表的显示方式，移动端建议使用卡片模式"
    >
      <Radio.Group
        value={displayMode}
        onChange={event => setDisplayMode(event.target.value)}
        optionType="button"
        buttonStyle="solid"
        options={[
          { label: '表格模式', value: 'table' },
          { label: '卡片模式', value: 'card' },
        ]}
      />

      <div className="mt-5">
        <Button type="primary" onClick={handleSave} loading={saving}>保存</Button>
      </div>
      {msg && (
        <Alert className="mt-4" type={msg.includes('成功') ? 'success' : 'error'} showIcon message={msg} />
      )}
    </Panel>
  )
}
