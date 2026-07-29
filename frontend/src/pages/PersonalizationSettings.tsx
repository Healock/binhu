import { useEffect, useState } from 'react'
import { Alert, Button, Radio } from 'antd'
import DockConfigurator from '../components/DockConfigurator'
import { Panel } from '../components/ui'
import { useAuth } from '../context/AuthContext'
import {
  defaultMobileDockConfig,
  normalizeMobileDockConfig,
} from '../navigation/mobileNavigation'
import type {
  MobileDockConfig,
  MobileNavigationMode,
  ReportColumnMode,
} from '../types'

export default function PersonalizationSettings() {
  const { user, updatePreferences } = useAuth()
  const [columnMode, setColumnMode] = useState<ReportColumnMode>('three')
  const [navigationMode, setNavigationMode] = (
    useState<MobileNavigationMode>('dock')
  )
  const [dockConfig, setDockConfig] = useState<MobileDockConfig>({
    groups: [],
  })
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    if (!user) return
    setColumnMode(user.report_column_mode || 'three')
    setNavigationMode(user.mobile_navigation_mode || 'dock')
    setDockConfig(normalizeMobileDockConfig(
      user.mobile_dock_config || defaultMobileDockConfig(user.role),
      user.role,
    ))
  }, [user])

  const handleSave = async () => {
    setSaving(true)
    setMsg('')
    try {
      await updatePreferences({
        report_column_mode: columnMode,
        mobile_navigation_mode: navigationMode,
        mobile_dock_config: dockConfig,
      })
      setMsg('保存成功，手机导航和汇总表设置已更新')
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
          <div className="mb-2 text-sm font-medium text-slate-800">
            手机导航方式
          </div>
          <Radio.Group
            value={navigationMode}
            onChange={event => setNavigationMode(event.target.value)}
            optionType="button"
            buttonStyle="solid"
            options={[
              { label: '浮空 Dock', value: 'dock' },
              { label: '侧边栏', value: 'sidebar' },
            ]}
          />
          <p className="mt-2 text-sm text-slate-500">
            电脑端始终使用左侧栏；这里仅控制手机端导航。
          </p>
        </div>

        {navigationMode === 'dock' && user && (
          <div>
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <div className="text-sm font-medium text-slate-800">
                Dock 分类和页面
              </div>
              <Button
                size="small"
                onClick={() => setDockConfig(
                  defaultMobileDockConfig(user.role),
                )}
              >
                恢复默认
              </Button>
            </div>
            <p className="mb-3 text-sm text-slate-500">
              把分类和页面拖进预览区即可调整；加号、移除和上下移动按钮也能完成相同操作。
            </p>
            <DockConfigurator
              value={dockConfig}
              role={user.role}
              onChange={setDockConfig}
            />
          </div>
        )}

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
