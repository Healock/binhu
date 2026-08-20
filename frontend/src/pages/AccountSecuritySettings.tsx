import { useEffect, useState } from 'react'
import { Alert, Button, Form, Input, List, Popconfirm, Tag, message } from 'antd'
import { LockOutlined, UserOutlined } from '@ant-design/icons'
import { Panel } from '../components/ui'
import { useAuth } from '../context/AuthContext'
import {
  getAuthSessions,
  revokeAllAuthSessions,
  revokeAuthSession,
  revokeOtherAuthSessions,
  type AuthSessionItem,
} from '../api/client'

interface PasswordFormValues {
  currentPassword: string
  newPassword: string
  confirmPassword: string
}

function errorMessage(error: any): string {
  const detail = error?.response?.data?.detail
  return typeof detail === 'object'
    ? detail?.message || '密码修改失败'
    : detail || '密码修改失败'
}

export default function AccountSecuritySettings() {
  const { user, changePassword } = useAuth()
  const [form] = Form.useForm<PasswordFormValues>()
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
    void loadSessions()
  }, [])

  const formatSessionTime = (value: string | null) => {
    if (!value) return '未知'
    return new Date(value).toLocaleString('zh-CN', { hour12: false })
  }

  const submitPassword = async (values: PasswordFormValues) => {
    try {
      await changePassword(values.currentPassword, values.newPassword)
      form.resetFields()
      message.success('密码已修改，请使用新密码重新登录')
      window.location.href = '/login'
    } catch (error) {
      message.error(errorMessage(error))
    }
  }

  return (
    <div className="account-security-settings">
      <Panel title="账号与安全" description="管理当前账号的登录密码和已登录设备。">
        <div className="account-security-settings__sections">
          <section className="account-security-settings__section">
            <div>
              <div className="text-sm font-medium text-[var(--app-text-strong)]">修改密码</div>
              <p className="mt-1 text-sm text-[var(--app-text-secondary)]">修改后全部设备都会退出，需要使用新密码重新登录。</p>
            </div>
            {user?.password_is_temporary && (
              <Alert type="warning" showIcon message="当前账号仍在使用临时密码，建议尽快修改。" />
            )}
            <Form<PasswordFormValues>
              form={form}
              className="max-w-lg"
              layout="vertical"
              requiredMark={false}
              onFinish={submitPassword}
            >
              <Form.Item label="当前密码" name="currentPassword" rules={[{ required: true, message: '请输入当前密码' }]}>
                <Input.Password prefix={<UserOutlined />} autoComplete="current-password" />
              </Form.Item>
              <Form.Item
                label="新密码"
                name="newPassword"
                rules={[{ required: true, message: '请输入新密码' }, { min: 8, message: '新密码至少 8 个字符' }]}
              >
                <Input.Password prefix={<LockOutlined />} autoComplete="new-password" />
              </Form.Item>
              <Form.Item
                label="确认新密码"
                name="confirmPassword"
                dependencies={['newPassword']}
                rules={[
                  { required: true, message: '请再次输入新密码' },
                  ({ getFieldValue }) => ({
                    validator(_, value) {
                      if (!value || getFieldValue('newPassword') === value) return Promise.resolve()
                      return Promise.reject(new Error('两次输入的新密码不一致'))
                    },
                  }),
                ]}
              >
                <Input.Password prefix={<LockOutlined />} autoComplete="new-password" />
              </Form.Item>
              <Button type="primary" htmlType="submit" icon={<LockOutlined />}>保存新密码</Button>
            </Form>
          </section>

          <section className="account-security-settings__section">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="text-sm font-medium text-[var(--app-text-strong)]">登录设备</div>
                <p className="mt-1 text-sm text-[var(--app-text-secondary)]">
                  同一账号最多保留一台电脑和一台手机；设备标识只保存在当前浏览器中。
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button size="small" onClick={() => void loadSessions()} loading={sessionsLoading}>刷新</Button>
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
              className="rounded-xl border border-[var(--app-border)]"
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
          </section>
        </div>
      </Panel>
    </div>
  )
}
