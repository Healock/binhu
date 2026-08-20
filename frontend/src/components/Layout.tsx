import { useMemo, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import {
  CloseOutlined,
  LogoutOutlined,
  MenuOutlined,
  SettingOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { Alert, Avatar, Button, Popover } from 'antd'
import { useAuth } from '../context/AuthContext'
import { getUserDisplayName, ROLE_LABELS } from '../types'
import {
  accessibleNavigationGroups,
  normalizeMobileDockConfig,
  mobileNavigationItemLabel,
  routeIsActive,
} from '../navigation/mobileNavigation'
import MobileDock from './MobileDock'
import NavigationIcon from './NavigationIcon'
import NotificationCenter from './NotificationCenter'
import SessionTimeoutGuard from './SessionTimeoutGuard'
import OnlinePresenceIndicator from './OnlinePresenceIndicator'
import { confirmPendingNavigation } from '../utils/navigationGuard'

export default function Layout() {
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [accountOpen, setAccountOpen] = useState(false)
  const { user, logout, serverVersion } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const permissionGroupCodes = useMemo(
    () => user?.permission_groups?.map(group => group.code) || [],
    [user],
  )
  const menuGroups = useMemo(
    () => user
      ? accessibleNavigationGroups(
          user.role,
          user.permissions,
          permissionGroupCodes,
          user.member?.position,
        )
      : [],
    [permissionGroupCodes, user],
  )
  const mobileNavigationMode = user?.mobile_navigation_mode || 'dock'
  const mobileWorkbenchPosition = ['组员', '组长', '基础管控', '中队长']
    .includes(user?.member?.position || '')
  const dockConfig = useMemo(
    () => user
      ? normalizeMobileDockConfig(
          user.mobile_dock_config,
          user.role,
          user.permissions,
          permissionGroupCodes,
          user.member?.position,
        )
      : { groups: [] },
    [permissionGroupCodes, user],
  )

  const handleLogout = async () => {
    if (!confirmPendingNavigation()) return
    setAccountOpen(false)
    await logout()
    navigate('/login', { replace: true })
  }

  return (
    <div className="app-shell flex">
      <header className="md:hidden fixed inset-x-0 top-0 z-30 flex h-14 items-center gap-3 border-b border-slate-200 bg-white px-4">
        {mobileNavigationMode === 'sidebar' && (
          <button
            type="button"
            onClick={() => setSidebarOpen(true)}
            aria-label="打开导航菜单"
            className="flex h-10 w-10 items-center justify-center text-slate-600"
          >
            <MenuOutlined />
          </button>
        )}
        <button
          type="button"
          aria-label="返回仪表盘"
          className="flex min-w-0 items-center gap-2 border-0 bg-transparent p-0 text-left"
          onClick={() => {
            if (!confirmPendingNavigation()) return
            setAccountOpen(false)
            navigate('/')
          }}
        >
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-blue-700 text-sm font-semibold text-white">滨</span>
          <span className="font-semibold text-slate-800">滨湖智慧平台</span>
        </button>
        {user && (
          <Popover
            open={accountOpen}
            onOpenChange={setAccountOpen}
            trigger="click"
            placement="bottomRight"
            content={(
              <div className="w-48 space-y-3 p-1">
                <div>
                  <div className="font-medium text-slate-900">{getUserDisplayName(user)}</div>
                  <div className="text-xs text-slate-500">用户名：{user.username}</div>
                  <div className="text-xs text-slate-500">
                    {user.permission_group?.name || ROLE_LABELS[user.role] || user.role}
                  </div>
                </div>
                <Button
                  block
                  icon={<UserOutlined />}
                  onClick={() => {
                    if (!confirmPendingNavigation()) return
                    setAccountOpen(false)
                    navigate('/profile')
                  }}
                >
                  个人中心
                </Button>
                <Button
                  block
                  icon={<SettingOutlined />}
                  onClick={() => {
                    if (!confirmPendingNavigation()) return
                    setAccountOpen(false)
                    navigate('/settings/personalization')
                  }}
                >
                  个性化设置
                </Button>
                <Button block icon={<LogoutOutlined />} onClick={handleLogout}>
                  退出登录
                </Button>
              </div>
            )}
          >
            <button
              type="button"
              aria-label="打开账号菜单"
              className="ml-auto mr-16 flex items-center gap-2 rounded-full px-2 py-1 text-xs text-slate-500 hover:bg-slate-100"
            >
              <Avatar size={24} src={user.avatar_url || undefined} icon={<UserOutlined />}>
                {getUserDisplayName(user).slice(0, 1)}
              </Avatar>
              <span className="hidden sm:inline">{getUserDisplayName(user)}</span>
            </button>
          </Popover>
        )}
      </header>

      <OnlinePresenceIndicator />

      {sidebarOpen && (
        <button
          type="button"
          aria-label="关闭导航菜单"
          className="md:hidden fixed inset-0 z-40 h-auto w-full rounded-none bg-slate-950/35"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <aside
        className={`fixed inset-y-0 left-0 z-50 flex h-screen w-[232px] shrink-0 flex-col border-r border-slate-200 bg-white transition-transform duration-200 md:sticky md:top-0 md:translate-x-0 ${
          sidebarOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        <div className="flex h-16 shrink-0 items-center gap-3 border-b border-slate-200 px-4">
          <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-blue-700 text-sm font-semibold text-white">
            滨
          </span>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-slate-900">滨湖智慧平台</div>
            <div className="text-xs text-slate-500">数据管理中心 · v{serverVersion}</div>
          </div>
          <button
            type="button"
            onClick={() => setSidebarOpen(false)}
            aria-label="关闭导航菜单"
            className="ml-auto flex h-9 w-9 items-center justify-center text-slate-500 md:hidden"
          >
            <CloseOutlined />
          </button>
        </div>

        <nav className="flex-1 overflow-y-auto px-3 py-4" onClick={() => setSidebarOpen(false)}>
          {menuGroups.map((group) => (
            <div key={group.label} className="mb-5 last:mb-0">
              <div className="mb-1.5 px-3 text-[11px] font-semibold tracking-wide text-slate-400">
                {group.label}
              </div>
              <div className="space-y-1">
                {group.items.map((item) => (
                  <NavLink
                    key={item.path}
                    to={item.path}
                    end={item.end}
                    onClick={(event) => {
                      if (!confirmPendingNavigation()) event.preventDefault()
                    }}
                    className={() =>
                      `flex min-h-10 items-center gap-3 rounded-lg px-3 text-sm font-medium transition-colors ${
                        routeIsActive(location.pathname, item)
                          ? 'bg-blue-50 text-blue-700'
                          : 'text-slate-600 hover:bg-slate-50 hover:text-slate-900'
                      }`
                    }
                  >
                    <span className="flex w-5 justify-center text-base">
                      <NavigationIcon name={item.icon} />
                    </span>
                    <span className={mobileWorkbenchPosition ? 'hidden md:inline' : ''}>{item.label}</span>
                    {mobileWorkbenchPosition && (
                      <span className="md:hidden">{mobileNavigationItemLabel(item, user.member.position)}</span>
                    )}
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </nav>

        {user && (
          <div className="shrink-0 border-t border-slate-200 p-3">
            <div className="flex items-center gap-2.5 px-1 py-1">
              <Avatar
                size={36}
                src={user.avatar_url || undefined}
                icon={<UserOutlined />}
                className="shrink-0 bg-slate-100 text-slate-600"
              >
                {getUserDisplayName(user).slice(0, 1)}
              </Avatar>
              <button
                type="button"
                onClick={() => {
                  if (confirmPendingNavigation()) navigate('/profile')
                }}
                className="min-w-0 flex-1 border-0 bg-transparent p-0 text-left"
              >
                <div className="truncate text-sm font-semibold text-slate-800">{getUserDisplayName(user)}</div>
                <div className="mt-0.5 truncate text-xs text-slate-500">
                  {user.username} · {user.permission_group?.name || ROLE_LABELS[user.role] || user.role}
                </div>
              </button>
              <NotificationCenter />
            </div>
            <button
              type="button"
              onClick={handleLogout}
              className="mt-2 flex w-full items-center justify-center gap-2 border-0 bg-slate-50 text-sm text-slate-600 transition-colors hover:bg-slate-100 hover:text-slate-900"
            >
              <LogoutOutlined />
              退出登录
            </button>
          </div>
        )}
      </aside>

      <main className="min-w-0 flex-1 overflow-auto">
        <div className={`app-content p-4 pt-[72px] md:p-6 ${
          mobileNavigationMode === 'dock'
            ? 'app-content--mobile-dock'
            : ''
        }`}>
          {user?.password_is_temporary && (
            <Alert
              className="app-temporary-password-alert"
              type="warning"
              showIcon
              message="当前账号仍在使用临时密码"
              description="请进入个人中心修改密码。系统不会强制修改，但会持续提醒。"
              action={<Button size="small" onClick={() => {
                if (confirmPendingNavigation()) navigate('/profile')
              }}>前往修改</Button>}
            />
          )}
          <div key={location.pathname} className="app-route-transition">
            <Outlet />
          </div>
          <SessionTimeoutGuard />
        </div>
      </main>

      {user && mobileNavigationMode === 'dock' && (
        <MobileDock
          config={dockConfig}
          role={user.role}
          permissions={user.permissions}
          position={user.member?.position}
          permissionGroupCodes={permissionGroupCodes}
        />
      )}
    </div>
  )
}
