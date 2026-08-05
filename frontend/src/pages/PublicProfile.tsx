import { useEffect, useState } from 'react'
import { Button, Empty, Select, Skeleton, Tag } from 'antd'
import { ArrowLeftOutlined, CalendarOutlined } from '@ant-design/icons'
import { useNavigate, useParams } from 'react-router-dom'
import { getPublicProfile } from '../api/client'
import type { PublicProfile as PublicProfileType } from '../types'
import ContributionCalendar from '../components/ContributionCalendar'
import { PageHeader, Panel } from '../components/ui'

function profileError(error: any): string {
  return error?.response?.data?.detail || '个人主页加载失败'
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
      setError('个人主页地址无效')
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
    return <div className="app-page"><div className="app-card p-6"><Skeleton active avatar paragraph={{ rows: 8 }} /></div></div>
  }
  if (error || !profile) {
    return (
      <div className="app-page">
        <PageHeader title="个人主页" />
        <div className="app-card p-8 text-center text-[var(--app-danger)]">{error || '个人主页不存在'}</div>
      </div>
    )
  }

  const maximum = Math.max(...profile.contribution.categories.map(item => item.count), 1)

  return (
    <div className="app-page">
      <PageHeader
        title="个人主页"
        description="仅展示内部公开资料和平台记录的实际工作贡献。"
        actions={<Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/people')}>返回人员主页</Button>}
      />

      <section className="app-card app-card--padded">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center">
          <span className="public-profile-avatar h-20 w-20 text-3xl">{profile.display_name.slice(0, 1)}</span>
          <div className="min-w-0 flex-1">
            <h1 className="text-2xl font-semibold text-[var(--app-text-strong)]">{profile.display_name}</h1>
            <div className="mt-2 flex flex-wrap gap-2">
              <Tag color="blue">{profile.position || '平台账号'}</Tag>
              {profile.departments.map(item => <Tag key={item}>{item}</Tag>)}
            </div>
            {profile.joined_at && (
              <p className="mt-3 text-sm text-[var(--app-text-secondary)]">
                <CalendarOutlined className="mr-1.5" />
                {new Date(profile.joined_at).toLocaleDateString('zh-CN')} 加入平台
              </p>
            )}
          </div>
        </div>
      </section>

      <Panel
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
        <div className="mt-5 grid grid-cols-3 gap-3 border-t border-[var(--app-border)] pt-4">
          {[
            ['实际工作', profile.contribution.total],
            ['活跃天数', profile.contribution.active_days],
            ['最长连续', `${profile.contribution.longest_streak} 天`],
          ].map(([label, value]) => (
            <div key={String(label)} className="rounded-lg bg-[var(--app-surface-muted)] p-3 text-center">
              <div className="text-xl font-semibold text-[var(--app-text-strong)]">{value}</div>
              <div className="mt-1 text-xs text-[var(--app-text-secondary)]">{label}</div>
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="工作构成" description="同一次提交即使修改多个实际工作字段，也只计算一次。">
        {profile.contribution.categories.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="该年度暂无实际工作记录" />
        ) : (
          <div className="space-y-4">
            {profile.contribution.categories.map(item => (
              <div key={item.type}>
                <div className="mb-1.5 flex items-center justify-between gap-3 text-sm">
                  <span className="font-medium text-[var(--app-text)]">{item.label}</span>
                  <span className="text-[var(--app-text-secondary)]">{item.count}</span>
                </div>
                <div className="h-2 overflow-hidden rounded-full bg-[var(--contribution-0)]">
                  <div
                    className="h-full rounded-full bg-[var(--app-primary)]"
                    style={{ width: `${Math.max(4, Math.round(item.count / maximum * 100))}%` }}
                  />
                </div>
              </div>
            ))}
          </div>
        )}
      </Panel>
    </div>
  )
}
