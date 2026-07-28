import { useState, type ReactNode } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import {
  ApartmentOutlined,
  BarChartOutlined,
  CloseOutlined,
  DatabaseOutlined,
  LogoutOutlined,
  MenuOutlined,
  MonitorOutlined,
  ReadOutlined,
  SearchOutlined,
  SettingOutlined,
  TeamOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { useAuth } from '../context/AuthContext'
import { ROLE_LABELS } from '../types'
import NotificationCenter from './NotificationCenter'

interface MenuItem {
  path: string
  label: string
  icon: ReactNode
  end?: boolean
}

interface MenuGroup {
  label: string
  items: MenuItem[]
}

export default function Layout() {
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const { user, logout } = useAuth()
  const navigate = useNavigate()

  const menuGroups: MenuGroup[] = [
    {
      label: '数据工作台',
      items: [
        { path: '/', label: '在线数据汇总', icon: <BarChartOutlined />, end: true },
        { path: '/query', label: '在线数据查询', icon: <SearchOutlined /> },
        { path: '/visit-summary', label: '走访汇总', icon: <ReadOutlined /> },
      ],
    },
    {
      label: '基础资料',
      items: [
        { path: '/grid-members', label: '网格员管理', icon: <TeamOutlined /> },
        { path: '/communities', label: '社区管理', icon: <ApartmentOutlined /> },
        ...(user?.role === 'super_admin'
          ? [{ path: '/users', label: '用户管理', icon: <UserOutlined /> }]
          : []),
      ],
    },
    {
      label: '系统',
      items: [
        ...(user?.role === 'super_admin'
          ? [{ path: '/operations', label: '运维中心', icon: <MonitorOutlined /> }]
          : []),
        { path: '/settings', label: '设置', icon: <SettingOutlined /> },
      ],
    },
  ]

  const handleLogout = async () => {
    await logout()
    navigate('/login', { replace: true })
  }

  return (
    <div className="app-shell flex">
      <header className="md:hidden fixed inset-x-0 top-0 z-30 flex h-14 items-center gap-3 border-b border-slate-200 bg-white px-4">
        <button
          type="button"
          onClick={() => setSidebarOpen(true)}
          aria-label="打开导航菜单"
          className="flex h-10 w-10 items-center justify-center text-slate-600"
        >
          <MenuOutlined />
        </button>
        <div className="flex items-center gap-2">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-blue-700 text-sm font-semibold text-white">滨</span>
          <span className="font-semibold text-slate-800">滨湖智慧平台</span>
        </div>
        {user && (
          <span className={`ml-auto text-xs text-slate-500 ${user.role === 'super_admin' ? 'mr-11' : ''}`}>
            {ROLE_LABELS[user.role] || user.role}
          </span>
        )}
      </header>

      {user?.role === 'super_admin' && <NotificationCenter />}

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
            <div className="text-xs text-slate-500">数据管理中心</div>
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
                    <span className="flex w-5 justify-center text-base">{item.icon}</span>
                    <span>{item.label}</span>
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </nav>

        {user && (
          <div className="shrink-0 border-t border-slate-200 p-3">
            <div className="rounded-lg bg-slate-50 p-3">
              <div className="flex items-center gap-2">
                <span className="flex h-8 w-8 items-center justify-center rounded-full bg-slate-200 text-slate-600">
                  <UserOutlined />
                </span>
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium text-slate-800">{user.username}</div>
                  <div className="text-xs text-slate-500">{ROLE_LABELS[user.role] || user.role}</div>
                </div>
              </div>
              <button
                type="button"
                onClick={handleLogout}
                className="mt-3 flex w-full items-center justify-center gap-2 border border-slate-200 bg-white text-xs text-slate-600 hover:border-slate-300 hover:text-slate-900"
              >
                <LogoutOutlined />
                退出登录
              </button>
            </div>
          </div>
        )}
      </aside>

      <main className="min-w-0 flex-1 overflow-auto">
        <div className="app-content p-4 pt-[72px] md:p-6">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
