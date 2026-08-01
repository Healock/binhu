import { Alert, Button, Descriptions, Form, Input, Tag, message } from 'antd'
import { LockOutlined, UserOutlined } from '@ant-design/icons'
import { useAuth } from '../context/AuthContext'
import { getUserDisplayName } from '../types'
import { PageHeader, Panel } from '../components/ui'

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

export default function Profile() {
  const { user, changePassword } = useAuth()
  const [form] = Form.useForm<PasswordFormValues>()

  if (!user) return null

  const submitPassword = async (values: PasswordFormValues) => {
    try {
      await changePassword(values.currentPassword, values.newPassword)
      form.resetFields()
      message.success('密码已修改')
    } catch (error) {
      message.error(errorMessage(error))
    }
  }

  return (
    <div className="app-page">
      <PageHeader
        title="个人中心"
        description="查看当前账号资料并管理自己的登录密码"
      />
      <div className="grid gap-5 xl:grid-cols-2">
        <Panel
          title="账号资料"
          description="姓名用于平台显示，用户名只用于登录。"
        >
          <Descriptions column={1} size="middle" colon={false}>
            <Descriptions.Item label="姓名">
              <span className="font-medium">{getUserDisplayName(user)}</span>
            </Descriptions.Item>
            <Descriptions.Item label="登录用户名">{user.username}</Descriptions.Item>
            <Descriptions.Item label="权限组">
              <div className="flex flex-wrap gap-1.5">
                {(user.permission_groups?.length
                  ? user.permission_groups
                  : user.permission_group ? [user.permission_group] : []
                ).map(group => <Tag key={group.id ?? group.code}>{group.name}</Tag>)}
                {!user.permission_groups?.length && !user.permission_group && '-'}
              </div>
            </Descriptions.Item>
            <Descriptions.Item label="所属部门">
              {user.departments?.map(item => item.name).join('、') || user.department?.name || '未分配'}
            </Descriptions.Item>
            <Descriptions.Item label="岗位">{user.member?.position || '-'}</Descriptions.Item>
            <Descriptions.Item label="密码状态">
              {user.password_is_temporary
                ? <Tag color="orange">临时密码</Tag>
                : <Tag color="green">已修改</Tag>}
            </Descriptions.Item>
          </Descriptions>
          <Alert
            className="mt-4"
            type="info"
            showIcon
            message="如需修改姓名，请联系超级管理员在用户管理中调整。"
          />
        </Panel>

        <Panel title="修改密码" description="修改后请使用新密码登录。">
          {user.password_is_temporary && (
            <Alert
              className="mb-5"
              type="warning"
              showIcon
              message="当前账号仍在使用临时密码，建议尽快修改。"
            />
          )}
          <Form<PasswordFormValues>
            form={form}
            layout="vertical"
            requiredMark={false}
            onFinish={submitPassword}
          >
            <Form.Item
              label="当前密码"
              name="currentPassword"
              rules={[{ required: true, message: '请输入当前密码' }]}
            >
              <Input.Password
                prefix={<UserOutlined />}
                autoComplete="current-password"
              />
            </Form.Item>
            <Form.Item
              label="新密码"
              name="newPassword"
              rules={[
                { required: true, message: '请输入新密码' },
                { min: 8, message: '新密码至少 8 个字符' },
              ]}
            >
              <Input.Password
                prefix={<LockOutlined />}
                autoComplete="new-password"
              />
            </Form.Item>
            <Form.Item
              label="确认新密码"
              name="confirmPassword"
              dependencies={['newPassword']}
              rules={[
                { required: true, message: '请再次输入新密码' },
                ({ getFieldValue }) => ({
                  validator(_, value) {
                    if (!value || getFieldValue('newPassword') === value) {
                      return Promise.resolve()
                    }
                    return Promise.reject(new Error('两次输入的新密码不一致'))
                  },
                }),
              ]}
            >
              <Input.Password
                prefix={<LockOutlined />}
                autoComplete="new-password"
              />
            </Form.Item>
            <Button type="primary" htmlType="submit" icon={<LockOutlined />}>
              保存新密码
            </Button>
          </Form>
        </Panel>
      </div>
    </div>
  )
}
