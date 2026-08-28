import { useEffect, useState } from 'react'
import { Alert, Button, Descriptions, Select, Skeleton, Tag, Upload, message } from 'antd'
import { UploadOutlined, UserOutlined } from '@ant-design/icons'
import { useAuth } from '../context/AuthContext'
import { getUserDisplayName, type PublicProfile } from '../types'
import { getPublicProfile, uploadAvatar } from '../api/client'
import { PageHeader, Panel } from '../components/ui'
import ContributionCalendar from '../components/ContributionCalendar'
import { AuthenticatedAvatar } from '../components/AuthenticatedImage'

function errorMessage(error: any, fallback = '操作失败'): string {
  const detail = error?.response?.data?.detail
  return typeof detail === 'object'
    ? detail?.message || fallback
    : detail || fallback
}

export default function Profile() {
  const { user, refreshUser } = useAuth()
  const [year, setYear] = useState(new Date().getFullYear())
  const [publicProfile, setPublicProfile] = useState<PublicProfile | null>(null)
  const [contributionLoading, setContributionLoading] = useState(true)
  const [avatarUploading, setAvatarUploading] = useState(false)

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

  const handleAvatarUpload = async (file: File) => {
    if (avatarUploading) return false
    setAvatarUploading(true)
    try {
      await uploadAvatar(file)
      await refreshUser()
      message.success('头像已更新')
    } catch (error) {
      message.error(errorMessage(error, '头像上传失败'))
    } finally {
      setAvatarUploading(false)
    }
    return false
  }

  return (
    <div className="app-page">
      <PageHeader
        title="个人中心"
        description="查看当前账号资料和个人工作贡献"
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
      <div>
        <Panel
          title="账号资料"
          description="姓名用于平台显示，用户名只用于登录。"
        >
          <div className="profile-avatar-editor">
            <AuthenticatedAvatar
              size={72}
              src={user.avatar_url}
              icon={<UserOutlined />}
            >
              {getUserDisplayName(user).slice(0, 1)}
            </AuthenticatedAvatar>
            <div className="profile-avatar-editor__content">
              <div className="font-medium">个人头像</div>
              <div className="text-sm text-[var(--app-text-secondary)]">支持 JPG、PNG、WebP 或 HEIC，最大 5MB</div>
              <Upload
                accept=".jpg,.jpeg,.png,.webp,.heic"
                disabled={avatarUploading}
                showUploadList={false}
                beforeUpload={handleAvatarUpload}
              >
                <Button
                  icon={<UploadOutlined />}
                  loading={avatarUploading}
                  disabled={avatarUploading}
                >
                  {user.avatar_url ? '更换头像' : '上传头像'}
                </Button>
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

      </div>
    </div>
  )
}
