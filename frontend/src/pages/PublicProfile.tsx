import { useEffect, useState } from 'react'
import { Button, Empty, Select, Skeleton, Tag } from 'antd'
import { ArrowLeftOutlined, CalendarOutlined } from '@ant-design/icons'
import { useNavigate, useParams } from 'react-router-dom'
import { getPublicProfile } from '../api/client'
import type { PublicProfile as PublicProfileType } from '../types'
import ContributionCalendar from '../components/ContributionCalendar'
import { PageHeader, Panel } from '../components/ui'

function profileError(error: any): string {
  return error?.response?.data?.detail || '个人资料加载失败'
}

export default function PublicProfile() {
  const navigate = useNavigate()
  const { userId } = useParams()
  const profileId = Number(userId)
  const [year, setYear] = useState(new Date().getFullYear())
  const [profile, setProfile] = useState<PublicProfileType | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!Number.isInteger(profileId) || profileId <= 0) {
      setError('个人资料地址无效')
      setLoading(false)
      return
    }
    let cancelled = false
    setLoading(true)
    setError('')
    getPublicProfile(profileId, year)
      .then((response) => { if (!cancelled) setProfile(response) })
      .catch((requestError) => { if (!cancelled) setError(profileError(requestError)) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [profileId, year])

  if (loading && !profile) {
    return (
      <div className="app-page public-profile-page">
        <div className="app-card p-6">
          <Skeleton active avatar paragraph={{ rows: 7 }} />
        </div>
      </div>
    )
  }

  if (error || !profile) {
    return (
      <div className="app-page public-profile-page">
        <PageHeader
          title="个人资料"
          actions={(
            <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/grid-members')}>
              返回人员管理
            </Button>
          )}
        />
        <div className="app-card p-8 text-center text-[var(--app-danger)]">
          {error || '个人资料不存在'}
        </div>
      </div>
    )
  }

  const maximum = Math.max(...profile.contribution.categories.map(item => item.count), 1)

  return (
    <div className="app-page public-profile-page">
      <PageHeader
        title="个人资料"
        description="内部公开资料与平台记录的实际工作贡献"
        actions={(
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/grid-members')}>
            返回人员管理
          </Button>
        )}
      />

      <div className="public-profile-layout">
        <aside className="public-profile-sidebar">
          <section className="app-card public-profile-identity">
            <div className="public-profile-identity__header">
              <span className="public-profile-avatar">{profile.display_name.slice(0, 1)}</span>
              <div className="min-w-0">
                <h1 className="truncate text-xl font-semibold text-[var(--app-text-strong)]">
                  {profile.display_name}
                </h1>
                <p className="mt-1 text-sm text-[var(--app-text-secondary)]">
                  {profile.position || '平台账号'}
                </p>
              </div>
            </div>
            {profile.departments.length > 0 && (
              <div className="public-profile-identity__tags">
                {profile.departments.map(item => <Tag key={item}>{item}</Tag>)}
              </div>
            )}
            {profile.joined_at && (
              <div className="public-profile-identity__joined">
                <CalendarOutlined />
                <span>{new Date(profile.joined_at).toLocaleDateString('zh-CN')} 加入平台</span>
              </div>
            )}
          </section>

          <Panel
            className="public-profile-breakdown"
            title="工作构成"
            description="一次提交修改多个实际工作字段，也只计一次。"
          >
            {profile.contribution.categories.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="该年度暂无实际工作" />
            ) : (
              <div className="public-profile-breakdown__list">
                {profile.contribution.categories.map(item => (
                  <div key={item.type} className="public-profile-breakdown__item">
                    <div className="flex items-center justify-between gap-3 text-sm">
                      <span className="font-medium text-[var(--app-text)]">{item.label}</span>
                      <strong className="text-[var(--app-text-strong)]">{item.count}</strong>
                    </div>
                    <div className="public-profile-breakdown__track">
                      <div
                        className="public-profile-breakdown__bar"
                        style={{ width: `${Math.max(4, Math.round(item.count / maximum * 100))}%` }}
                      />
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Panel>
        </aside>

        <Panel
          className="public-profile-contribution"
          title={`${profile.year} 年工作贡献`}
          description="只统计成功完成的核查处理、下发审核和工作日志编制。"
          extra={(
            <Select
              value={year}
              onChange={setYear}
              options={profile.available_years.map(item => ({ value: item, label: `${item} 年` }))}
              style={{ width: 112 }}
            />
          )}
        >
          <ContributionCalendar year={profile.year} days={profile.contribution.days} />
          <div className="public-profile-stats">
            {[
              ['实际工作', profile.contribution.total],
              ['活跃天数', profile.contribution.active_days],
              ['最长连续', `${profile.contribution.longest_streak} 天`],
            ].map(([label, value]) => (
              <div key={String(label)} className="public-profile-stat">
                <div className="public-profile-stat__value">{value}</div>
                <div className="public-profile-stat__label">{label}</div>
              </div>
            ))}
          </div>
        </Panel>
      </div>
    </div>
  )
}
