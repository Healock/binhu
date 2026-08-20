import { useEffect, useMemo, useState } from 'react'
import { Avatar, Drawer, Input, List, Popover, Tag } from 'antd'
import { TeamOutlined, UserOutlined } from '@ant-design/icons'
import { getPresenceUsers, sendPresenceHeartbeat } from '../api/client'
import { useAuth } from '../context/AuthContext'
import useMobileViewport from '../hooks/useMobileViewport'
import type { PresenceUser } from '../types'

const CLIENT_STORAGE_KEY = 'binhu_presence_client_id'
const HEARTBEAT_INTERVAL_MS = 30_000

function getClientId(): string {
  const existing = window.localStorage.getItem(CLIENT_STORAGE_KEY)
  if (existing) return existing
  const generated = typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `presence-${Date.now()}-${Math.random().toString(36).slice(2)}`
  window.localStorage.setItem(CLIENT_STORAGE_KEY, generated)
  return generated
}

function displayLastSeen(value: string | null): string {
  if (!value) return '刚刚在线'
  const elapsed = Math.max(0, Date.now() - new Date(value).getTime())
  if (elapsed < 60_000) return '刚刚在线'
  return `${Math.floor(elapsed / 60_000)} 分钟前在线`
}

function PresenceUsersPanel({ users }: { users: PresenceUser[] }) {
  const [keyword, setKeyword] = useState('')
  const filtered = useMemo(() => {
    const normalized = keyword.trim().toLowerCase()
    if (!normalized) return users
    return users.filter(item => (
      `${item.display_name} ${item.position} ${item.department || ''}`
    ).toLowerCase().includes(normalized))
  }, [keyword, users])

  return (
    <div className="online-presence-popover">
      <div className="mb-2 flex items-center justify-between gap-3">
        <strong>当前在线</strong>
        <Tag color="success">{users.length} 人</Tag>
      </div>
      <Input.Search
        allowClear
        size="small"
        placeholder="筛选姓名"
        value={keyword}
        onChange={event => setKeyword(event.target.value)}
      />
      <div className="online-presence-users mt-2">
        <List
          size="small"
          dataSource={filtered}
          locale={{ emptyText: '暂无在线用户' }}
          renderItem={item => (
            <List.Item>
              <List.Item.Meta
                avatar={(
                  <Avatar src={item.avatar_url || undefined} icon={<UserOutlined />}>
                    {item.display_name.slice(0, 1)}
                  </Avatar>
                )}
                title={item.display_name}
                description={(
                  <span>
                    {[item.position, item.department].filter(Boolean).join(' · ') || '平台用户'}
                    <span className="ml-2 text-xs text-[var(--app-text-tertiary)]">
                      {displayLastSeen(item.last_seen_at)}
                    </span>
                  </span>
                )}
              />
            </List.Item>
          )}
        />
      </div>
    </div>
  )
}

export default function OnlinePresenceIndicator() {
  const { user } = useAuth()
  const mobile = useMobileViewport()
  const [onlineCount, setOnlineCount] = useState<number | null>(null)
  const [connected, setConnected] = useState(false)
  const [open, setOpen] = useState(false)
  const [users, setUsers] = useState<PresenceUser[]>([])
  const [loadingUsers, setLoadingUsers] = useState(false)
  const canViewDetails = Boolean(user?.permissions?.includes('presence.detail.view'))

  useEffect(() => {
    if (!user) {
      setOnlineCount(null)
      setConnected(false)
      return
    }
    const clientId = getClientId()
    let disposed = false
    const heartbeat = async () => {
      if (document.visibilityState !== 'visible') return
      try {
        const result = await sendPresenceHeartbeat(clientId)
        if (disposed) return
        setOnlineCount(result.online_count)
        setConnected(true)
      } catch {
        if (!disposed) setConnected(false)
      }
    }
    heartbeat()
    const timer = window.setInterval(heartbeat, HEARTBEAT_INTERVAL_MS)
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') heartbeat()
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      disposed = true
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [user])

  useEffect(() => {
    if (!open || !canViewDetails) return
    setLoadingUsers(true)
    getPresenceUsers()
      .then(result => setUsers(result.users))
      .catch(() => setUsers([]))
      .finally(() => setLoadingUsers(false))
  }, [canViewDetails, open])

  if (!user) return null
  const content = loadingUsers
    ? <div className="py-4 text-center text-sm text-[var(--app-text-secondary)]">正在读取在线名单…</div>
    : <PresenceUsersPanel users={users} />
  const indicator = (
    <button
      type="button"
      className={`online-presence-indicator ${canViewDetails ? 'is-clickable' : ''}`}
      onClick={mobile && canViewDetails ? () => setOpen(current => !current) : undefined}
      aria-label={canViewDetails ? '查看在线用户' : `当前在线 ${onlineCount ?? 0} 人`}
      disabled={!canViewDetails}
    >
      <span className={`online-presence-dot ${connected ? 'is-online' : 'is-offline'}`} />
      <TeamOutlined />
      <span>{onlineCount ?? '--'}</span>
    </button>
  )

  if (mobile) {
    return (
      <>
        {indicator}
        <Drawer
          title="当前在线用户"
          placement="bottom"
          height="min(72vh, 560px)"
          open={open}
          onClose={() => setOpen(false)}
          destroyOnClose
        >
          {content}
        </Drawer>
      </>
    )
  }
  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      trigger="click"
      placement="bottomRight"
      content={content}
    >
      {indicator}
    </Popover>
  )
}
