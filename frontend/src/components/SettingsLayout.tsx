import { NavLink, Outlet, Navigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

export default function SettingsLayout() {
  const { user } = useAuth()
  const isSuperAdmin = user?.role === 'super_admin'

  const allMenuItems = [
    { path: '/settings/spreadsheets', label: '在线表格配置', superOnly: true },
    { path: '/settings/oauth', label: '腾讯文档OAuth', superOnly: true },
    { path: '/settings/system', label: '系统设置', superOnly: true },
    { path: '/settings/personalization', label: '个性化', superOnly: false },
  ]

  // 非超管只看到"个性化"
  const menuItems = allMenuItems.filter(item => !item.superOnly || isSuperAdmin)

  // 非超管访问超管页面 → 重定向到个性化
  const currentPath = window.location.pathname
  const restrictedPaths = allMenuItems.filter(i => i.superOnly).map(i => i.path)
  if (!isSuperAdmin && restrictedPaths.some(p => currentPath.startsWith(p))) {
    return <Navigate to="/settings/personalization" replace />
  }

  const navLinkClass = ({ isActive }: { isActive: boolean }) =>
    isActive
      ? 'bg-blue-50 text-blue-700'
      : 'text-gray-600 hover:text-gray-900 hover:bg-gray-100'

  return (
    <div className="max-w-5xl mx-auto">
      <div className="md:flex md:gap-6">
        <aside className="hidden md:block w-48 shrink-0">
          <nav className="space-y-1">
            {menuItems.map((item) => (
              <NavLink key={item.path} to={item.path}
                className={({ isActive }) =>
                  `block px-4 py-2.5 rounded-lg text-sm font-medium transition-colors ${navLinkClass({ isActive })}`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
        </aside>

        <div className="md:hidden mb-4">
          <nav className="flex gap-1 overflow-x-auto pb-1">
            {menuItems.map((item) => (
              <NavLink key={item.path} to={item.path}
                className={({ isActive }) =>
                  `shrink-0 px-3 py-1.5 rounded-lg text-sm font-medium ${navLinkClass({ isActive })}`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
        </div>

        <div className="flex-1 min-w-0">
          <Outlet />
        </div>
      </div>
    </div>
  )
}
