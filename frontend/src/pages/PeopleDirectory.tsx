import { useEffect, useState } from 'react'
import { Empty, Input, Pagination, Select, Skeleton, Tag } from 'antd'
import { SearchOutlined, RightOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { listPublicProfiles } from '../api/client'
import type { PublicProfileSummary } from '../types'
import { PageHeader } from '../components/ui'

const POSITIONS = [
  '组员', '组长', '片长', '基础管控', '中队长',
  '社区民警', '所队领导', '自购房',
]

function profileError(error: any): string {
  return error?.response?.data?.detail || '人员主页加载失败'
}

export default function PeopleDirectory() {
  const navigate = useNavigate()
  const [profiles, setProfiles] = useState<PublicProfileSummary[]>([])
  const [keywordInput, setKeywordInput] = useState('')
  const [keyword, setKeyword] = useState('')
  const [position, setPosition] = useState('')
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [year, setYear] = useState(new Date().getFullYear())
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setKeyword(keywordInput.trim())
      setPage(1)
    }, 250)
    return () => window.clearTimeout(timer)
  }, [keywordInput])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    listPublicProfiles({ keyword, position, page, page_size: 24 })
      .then((response) => {
        if (cancelled) return
        setProfiles(response.data)
        setTotal(response.total)
        setYear(response.year)
      })
      .catch((requestError) => {
        if (!cancelled) setError(profileError(requestError))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [keyword, page, position])

  return (
    <div className="app-page">
      <PageHeader
        title="人员主页"
        description="查看平台人员的公开资料和实际工作贡献；登录、浏览、查询等普通操作不会计入。"
      />

      <section className="app-card app-toolbar grid gap-3 sm:grid-cols-[minmax(220px,1fr)_180px]">
        <Input
          value={keywordInput}
          onChange={event => setKeywordInput(event.target.value)}
          prefix={<SearchOutlined />}
          placeholder="搜索姓名、岗位或部门"
          allowClear
        />
        <Select
          value={position || undefined}
          onChange={(value) => { setPosition(value || ''); setPage(1) }}
          placeholder="全部岗位"
          allowClear
          options={POSITIONS.map(item => ({ value: item, label: item }))}
        />
      </section>

      {loading ? (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2, 3, 4, 5].map(item => (
            <div key={item} className="app-card p-5"><Skeleton active avatar paragraph={{ rows: 2 }} /></div>
          ))}
        </div>
      ) : error ? (
        <div className="app-card p-8 text-center text-[var(--app-danger)]">{error}</div>
      ) : profiles.length === 0 ? (
        <div className="app-card py-12"><Empty description="没有找到符合条件的人员" /></div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {profiles.map(profile => (
            <button
              key={profile.id}
              type="button"
              className="app-card min-h-0 w-full border-0 p-5 text-left transition-transform hover:-translate-y-0.5"
              onClick={() => navigate(`/people/${profile.id}`)}
            >
              <div className="flex items-start gap-3">
                <span className="public-profile-avatar h-12 w-12 text-lg">
                  {profile.display_name.slice(0, 1)}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <h2 className="truncate text-base font-semibold text-[var(--app-text-strong)]">
                        {profile.display_name}
                      </h2>
                      <div className="mt-1 flex flex-wrap gap-1.5">
                        <Tag color="blue">{profile.position || '平台账号'}</Tag>
                      </div>
                    </div>
                    <RightOutlined className="mt-1 text-[var(--app-text-muted)]" />
                  </div>
                  <p className="mt-2 truncate text-sm text-[var(--app-text-secondary)]">
                    {profile.departments.join('、') || '未分配部门'}
                  </p>
                </div>
              </div>
              <div className="mt-4 grid grid-cols-2 gap-3 border-t border-[var(--app-border)] pt-4">
                <div>
                  <div className="text-xl font-semibold text-[var(--app-primary)]">{profile.contribution.total}</div>
                  <div className="mt-0.5 text-xs text-[var(--app-text-secondary)]">{year} 年实际工作</div>
                </div>
                <div>
                  <div className="text-xl font-semibold text-[var(--app-text-strong)]">{profile.contribution.active_days}</div>
                  <div className="mt-0.5 text-xs text-[var(--app-text-secondary)]">活跃天数</div>
                </div>
              </div>
            </button>
          ))}
        </div>
      )}

      {total > 24 && (
        <div className="app-card flex justify-center p-3">
          <Pagination current={page} pageSize={24} total={total} showSizeChanger={false} onChange={setPage} />
        </div>
      )}
    </div>
  )
}
