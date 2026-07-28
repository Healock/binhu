import { useEffect, useState } from 'react'
import { Alert, Button, Radio } from 'antd'
import { Panel } from '../components/ui'
import { useAuth } from '../context/AuthContext'
import type { ReportColumnMode, TableDisplayMode } from '../types'

export default function PersonalizationSettings() {
  const { user, updatePreferences } = useAuth()
  const [displayMode, setDisplayMode] = useState<TableDisplayMode>('table')
  const [columnMode, setColumnMode] = useState<ReportColumnMode>('three')
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    if (!user) return
    setDisplayMode(user.table_display_mode || 'table')
    setColumnMode(user.report_column_mode || 'three')
  }, [user])

  const handleSave = async () => {
    setSaving(true)
    setMsg('')
    try {
      await updatePreferences({
        table_display_mode: displayMode,
        report_column_mode: columnMode,
      })
      localStorage.removeItem('table_display_mode')
      setMsg('保存成功，汇总页面会按新的方式显示')
    } catch {
      setMsg('保存失败，请稍后重试')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Panel
      title="个性化"
      description="这些设置跟随当前账号，在其他电脑登录后也会保持一致。"
    >
      <div className="space-y-6">
        <div>
          <div className="mb-2 text-sm font-medium text-slate-800">数据列表显示方式</div>
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
          <p className="mt-2 text-sm text-slate-500">
            该选项主要控制电脑端；在线数据汇总在手机端会自动使用精简列表，也可以临时切换为完整表格。
          </p>
        </div>

        <div>
          <div className="mb-2 text-sm font-medium text-slate-800">汇总表统计列</div>
          <Radio.Group
            value={columnMode}
            onChange={event => setColumnMode(event.target.value)}
            optionType="button"
            buttonStyle="solid"
            options={[
              { label: '三列模式', value: 'three' },
              { label: '两列模式', value: 'two' },
            ]}
          />
          <Alert
            className="mt-3"
            type="info"
            showIcon
            message={columnMode === 'three'
              ? '三列模式：未核查、已核查、已完成'
              : '两列模式：未核查、已核查'}
            description={columnMode === 'three'
              ? '“已核查”表示已经填写地址、但还没有填写最终核查结果。'
              : '原来的“未核查”和“已核查”合并为“未核查”；原来的“已完成”显示为“已核查”。'}
          />
        </div>
      </div>

      <div className="mt-5">
        <Button type="primary" onClick={handleSave} loading={saving}>
          保存设置
        </Button>
      </div>
      {msg && (
        <Alert
          className="mt-4"
          type={msg.includes('成功') ? 'success' : 'error'}
          showIcon
          message={msg}
        />
      )}
    </Panel>
  )
}
