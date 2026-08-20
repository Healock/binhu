import { useEffect, useState } from 'react'
import { Alert, Button, List, Popconfirm, Segmented, Tag, message } from 'antd'
import {
  DesktopOutlined,
  MoonOutlined,
  SunOutlined,
} from '@ant-design/icons'
import DockConfigurator from '../components/DockConfigurator'
import { Panel } from '../components/ui'
import { useAuth } from '../context/AuthContext'
import {
  defaultMobileDockConfig,
  normalizeMobileDockConfig,
} from '../navigation/mobileNavigation'
import {
  getAuthSessions,
  revokeAllAuthSessions,
  revokeAuthSession,
  revokeOtherAuthSessions,
  type AuthSessionItem,
} from '../api/client'
import type {
  MobileDockConfig,
  MobileNavigationMode,
  ReportColumnMode,
  TaskDisplayMode,
  ThemeMode,
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
  const [themeMode, setThemeMode] = useState<ThemeMode>('light')
  const [taskDisplayMode, setTaskDisplayMode] = useState<TaskDisplayMode>('card')
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')
  const [sessions, setSessions] = useState<AuthSessionItem[]>([])
  const [sessionsLoading, setSessionsLoading] = useState(false)

  const loadSessions = async () => {
    setSessionsLoading(true)
    try {
      setSessions(await getAuthSessions())
    } catch {
      setSessions([])
    } finally {
      setSessionsLoading(false)
    }
  }

  useEffect(() => {
    if (!user) return
    setColumnMode(user.report_column_mode || 'three')
    setNavigationMode(user.mobile_navigation_mode || 'dock')
    setThemeMode(user.theme_mode || 'light')
    setTaskDisplayMode(user.task_display_mode || 'card')
    setDockConfig(normalizeMobileDockConfig(
      user.mobile_dock_config || defaultMobileDockConfig(
        user.role,
        user.permissions,
        user.permission_groups?.map(group => group.code),
        user.member?.position,
      ),
      user.role,
      user.permissions,
      user.permission_groups?.map(group => group.code),
      user.member?.position,
    ))
  }, [user])

  useEffect(() => {
    loadSessions().catch(() => {})
  }, [])

  const formatSessionTime = (value: string | null) => {
    if (!value) return '未知'
    return new Date(value).toLocaleString('zh-CN', { hour12: false })
  }

  const handleSave = async () => {
    setSaving(true)
    setMsg('')
    try {
      await updatePreferences({
        theme_mode: themeMode,
        task_display_mode: taskDisplayMode,
        report_column_mode: columnMode,
        mobile_navigation_mode: navigationMode,
        mobile_dock_config: dockConfig,
      })
      setMsg('保存成功，外观、任务视图、手机导航和汇总表设置已更新')
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
      <div className="flex flex-col gap-7">
        <div className="flex flex-col items-start gap-2">
          <div className="text-sm font-medium text-slate-800">
            流口任务展示
          </div>
          <div className="w-full max-w-sm">
            <Segmented
              block
              size="large"
              value={taskDisplayMode}
              onChange={value => setTaskDisplayMode(value as TaskDisplayMode)}
              options={[
                { label: '卡片视图', value: 'card' },
                { label: '表格视图', value: 'table' },
              ]}
            />
          </div>
          <p className="text-sm text-slate-500">
            表格视图适合电脑端连续浏览和批量选择；手机端仍使用卡片。
          </p>
        </div>

        <div className="flex flex-col items-start gap-2">
          <div className="text-sm font-medium text-slate-800">
            外观模式
          </div>
          <div className="w-full max-w-lg">
            <Segmented
              block
              size="large"
              value={themeMode}
              onChange={value => setThemeMode(value as ThemeMode)}
              options={[
                {
                  label: <span className="inline-flex items-center gap-1.5"><SunOutlined />浅色</span>,
                  value: 'light',
                },
                {
                  label: <span className="inline-flex items-center gap-1.5"><MoonOutlined />深色</span>,
                  value: 'dark',
                },
                {
                  label: <span className="inline-flex items-center gap-1.5"><DesktopOutlined />跟随系统</span>,
                  value: 'system',
                },
              ]}
            />
          </div>
          <p className="text-sm text-slate-500">
            “跟随系统”会随着电脑或手机的外观设置自动切换。
          </p>
        </div>

        <div className="flex flex-col items-start gap-2">
          <div className="text-sm font-medium text-slate-800">
            手机导航方式
          </div>
          <div className="w-full max-w-sm">
            <Segmented
              block
              size="large"
              value={navigationMode}
              onChange={value => setNavigationMode(value as MobileNavigationMode)}
              options={[
                { label: '浮空 Dock', value: 'dock' },
                { label: '侧边栏', value: 'sidebar' },
              ]}
            />
          </div>
          <p className="text-sm text-slate-500">
            电脑端始终使用左侧栏；这里仅控制手机端导航。
          </p>
        </div>

        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <div className="text-sm font-medium text-slate-800">登录设备</div>
              <p className="mt-1 text-sm text-slate-500">
                同一账号最多保留一台电脑和一台手机；设备标识只保存在当前浏览器中。
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button size="small" onClick={() => loadSessions()} loading={sessionsLoading}>刷新</Button>
              <Popconfirm
                title="退出其他设备？"
                description="当前设备会继续保持登录。"
                onConfirm={async () => {
                  try {
                    await revokeOtherAuthSessions()
                    message.success('其他设备已退出')
                    await loadSessions()
                  } catch {
                    message.error('退出其他设备失败')
                  }
                }}
              >
                <Button size="small">退出其他设备</Button>
              </Popconfirm>
              <Popconfirm
                title="退出全部设备？"
                description="当前设备也会退出，需要重新登录。"
                onConfirm={async () => {
                  try {
                    await revokeAllAuthSessions()
                    window.location.href = '/login'
                  } catch {
                    message.error('退出全部设备失败')
                  }
                }}
              >
                <Button size="small" danger>退出全部设备</Button>
              </Popconfirm>
            </div>
          </div>
          <List
            loading={sessionsLoading}
            className="rounded-xl border border-slate-200"
            dataSource={sessions}
            locale={{ emptyText: '暂无有效登录设备' }}
            renderItem={item => (
              <List.Item
                actions={item.current ? [<Tag color="blue" key="current">当前设备</Tag>] : [
                  <Popconfirm
                    key="revoke"
                    title="退出这个设备？"
                    onConfirm={async () => {
                      try {
                        await revokeAuthSession(item.management_id)
                        message.success('设备已退出')
                        await loadSessions()
                      } catch {
                        message.error('退出设备失败')
                      }
                    }}
                  >
                    <Button type="link" danger size="small">退出</Button>
                  </Popconfirm>,
                ]}
              >
                <List.Item.Meta
                  title={(
                    <span className="inline-flex items-center gap-2">
                      {item.device_type === 'mobile' ? '手机端' : '电脑端'}
                      <Tag>{item.user_agent_family}</Tag>
                    </span>
                  )}
                  description={`最近活动：${formatSessionTime(item.last_activity_at)} · 到期：${formatSessionTime(item.expires_at)}`}
                />
              </List.Item>
            )}
          />
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
                  defaultMobileDockConfig(
                    user.role,
                    user.permissions,
                    user.permission_groups?.map(group => group.code),
                    user.member?.position,
                  ),
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
              permissions={user.permissions}
              position={user.member?.position}
              permissionGroupCodes={user.permission_groups?.map(group => group.code)}
              onChange={setDockConfig}
            />
          </div>
        )}

        <div className="flex flex-col items-start gap-2">
          <div className="text-sm font-medium text-slate-800">汇总表统计列</div>
          <div className="w-full max-w-sm">
            <Segmented
              block
              size="large"
              value={columnMode}
              onChange={value => setColumnMode(value as ReportColumnMode)}
              options={[
                { label: '三列模式', value: 'three' },
                { label: '两列模式', value: 'two' },
              ]}
            />
          </div>
          <div className="w-full pt-1">
            <Alert
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
      </div>

      <div className="mt-6 flex justify-end border-t border-slate-200 pt-5">
        <Button type="primary" onClick={handleSave} loading={saving}>
          保存设置
        </Button>
      </div>
      {msg && (
        <div className="mt-4">
          <Alert
            type={msg.includes('成功') ? 'success' : 'error'}
            showIcon
            message={msg}
          />
        </div>
      )}
    </Panel>
  )
}
