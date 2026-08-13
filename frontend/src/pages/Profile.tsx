import { useEffect, useState } from 'react'
import { Alert, Avatar, Button, Descriptions, Form, Input, Select, Skeleton, Tag, Upload, message } from 'antd'
import { LockOutlined, UploadOutlined, UserOutlined } from '@ant-design/icons'
import { useAuth } from '../context/AuthContext'
import { getUserDisplayName, type PublicProfile } from '../types'
import { getPublicProfile, uploadAvatar } from '../api/client'
import { PageHeader, Panel } from '../components/ui'
import ContributionCalendar from '../components/ContributionCalendar'

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
  const { user, changePassword, refreshUser } = useAuth()
  const [form] = Form.useForm<PasswordFormValues>()
  const [year, setYear] = useState(new Date().getFullYear())
  const [publicProfile, setPublicProfile] = useState<PublicProfile | null>(null)
  const [contributionLoading, setContributionLoading] = useState(true)

  useEffect(() => {
    if (!user) return
    let cancelled = false
    setContributionLoading(true)
    getPublicProfile(user.id, year)
      .then(response => { if (!cancelled) setPublicProfile(response) })
      .catch(() => { if (!cancelled) setPublicProfile(null) })
      .finally(() => { if (!cancelled) setContributionLoading(false) })
    return () => { cancelled = true }
  }, [user, year])

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

  const handleAvatarUpload = async (file: File) => {
    try {
      await uploadAvatar(file)
      await refreshUser()
      message.success('头像已更新')
    } catch (error) {
      message.error(errorMessage(error))
    }
    return false
  }

  return (
    <div className="app-page">
      <PageHeader
        title="个人中心"
        description="查看当前账号资料并管理自己的登录密码"
      />
      <Panel
        title="我的工作贡献"
        description="登录、浏览、查询、导出和任务分配等普通操作不会计入。"
        extra={(
          publicProfile && (
            <Select
              value={year}
              onChange={setYear}
              options={publicProfile.available_years.map(item => ({ value: item, label: `${item} 年` }))}
              style={{ width: 108 }}
            />
          )
        )}
      >
        {contributionLoading ? (
          <Skeleton active paragraph={{ rows: 3 }} />
        ) : publicProfile ? (
          <>
            <ContributionCalendar year={publicProfile.year} days={publicProfile.contribution.days} />
            <div className="mt-4 flex flex-wrap gap-x-6 gap-y-2 border-t border-[var(--app-border)] pt-4 text-sm text-[var(--app-text-secondary)]">
              <span><strong className="mr-1 text-[var(--app-text-strong)]">{publicProfile.contribution.total}</strong>项实际工作</span>
              <span><strong className="mr-1 text-[var(--app-text-strong)]">{publicProfile.contribution.active_days}</strong>个活跃日</span>
              <span>最长连续 <strong className="text-[var(--app-text-strong)]">{publicProfile.contribution.longest_streak}</strong> 天</span>
            </div>
          </>
        ) : (
          <Alert type="warning" showIcon message="工作贡献暂时无法加载" />
        )}
      </Panel>
      <div className="grid gap-5 xl:grid-cols-2">
        <Panel
          title="账号资料"
          description="姓名用于平台显示，用户名只用于登录。"
        >
          <div className="profile-avatar-editor">
            <Avatar
              size={72}
              src={user.avatar_url || undefined}
              icon={<UserOutlined />}
            >
              {getUserDisplayName(user).slice(0, 1)}
            </Avatar>
            <div className="profile-avatar-editor__content">
              <div className="font-medium">个人头像</div>
              <div className="text-sm text-[var(--app-text-secondary)]">支持 JPG、PNG、WebP 或 HEIC，最大 5MB</div>
              <Upload accept=".jpg,.jpeg,.png,.webp,.heic" showUploadList={false} beforeUpload={handleAvatarUpload}>
                <Button icon={<UploadOutlined />}>上传头像</Button>
              </Upload>
            </div>
          </div>
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
