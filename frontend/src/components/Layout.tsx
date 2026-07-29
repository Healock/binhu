import { useMemo, useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import {
  CloseOutlined,
  LogoutOutlined,
  MenuOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { Button, Popover } from 'antd'
import { useAuth } from '../context/AuthContext'
import { ROLE_LABELS } from '../types'
import {
  accessibleNavigationGroups,
  normalizeMobileDockConfig,
} from '../navigation/mobileNavigation'
import MobileDock from './MobileDock'
import NavigationIcon from './NavigationIcon'
import NotificationCenter from './NotificationCenter'

export default function Layout() {
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [accountOpen, setAccountOpen] = useState(false)
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const menuGroups = useMemo(
    () => user ? accessibleNavigationGroups(user.role) : [],
    [user],
  )
  const mobileNavigationMode = user?.mobile_navigation_mode || 'dock'
  const dockConfig = useMemo(
    () => user
      ? normalizeMobileDockConfig(user.mobile_dock_config, user.role)
      : { groups: [] },
    [user],
  )

  const handleLogout = async () => {
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
        <div className="flex items-center gap-2">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-blue-700 text-sm font-semibold text-white">滨</span>
          <span className="font-semibold text-slate-800">滨湖智慧平台</span>
        </div>
        {user && (
          <Popover
            open={accountOpen}
            onOpenChange={setAccountOpen}
            trigger="click"
            placement="bottomRight"
            content={(
              <div className="w-48 space-y-3 p-1">
                <div>
                  <div className="font-medium text-slate-900">{user.username}</div>
                  <div className="text-xs text-slate-500">
                    {ROLE_LABELS[user.role] || user.role}
                  </div>
                </div>
                <Button
                  block
                  onClick={() => {
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
              className={`ml-auto rounded-full px-2 py-1 text-xs text-slate-500 hover:bg-slate-100 ${user.role === 'super_admin' ? 'mr-11' : ''}`}
            >
              {ROLE_LABELS[user.role] || user.role}
            </button>
          </Popover>
        )}
      </header>

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
            <div className="text-xs text-slate-500">数据管理中心 · v{__APP_VERSION__}</div>
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
                    className={({ isActive }) =>
                      `flex min-h-10 items-center gap-3 rounded-lg px-3 text-sm font-medium transition-colors ${
                        isActive
                          ? 'bg-blue-50 text-blue-700'
                          : 'text-slate-600 hover:bg-slate-50 hover:text-slate-900'
                      }`
                    }
                  >
                    <span className="flex w-5 justify-center text-base">
                      <NavigationIcon name={item.icon} />
                    </span>
                    <span>{item.label}</span>
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </nav>

        {user && (
          <div className="shrink-0 border-t border-slate-200 p-3">
            <div className="flex items-center gap-2.5 px-1 py-1">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-slate-100 text-slate-600">
                <UserOutlined />
              </span>
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-semibold text-slate-800">{user.username}</div>
                <div className="mt-0.5 truncate text-xs text-slate-500">
                  {ROLE_LABELS[user.role] || user.role}
                </div>
              </div>
              {user.role === 'super_admin' && <NotificationCenter />}
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
          <Outlet />
        </div>
      </main>

      {user && mobileNavigationMode === 'dock' && (
        <MobileDock config={dockConfig} role={user.role} />
      )}
    </div>
  )
}
