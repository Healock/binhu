import { useState } from 'react'
import { Outlet, NavLink, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { ROLE_LABELS } from '../types'

export default function Layout() {
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const { user, logout } = useAuth()
  const navigate = useNavigate()

  const menuItems = [
    { path: '/', label: '在线数据汇总' },
    { path: '/query', label: '在线数据查询' },
    { path: '/grid-members', label: '网格员管理' },
    { path: '/communities', label: '社区管理' },
    // 用户管理仅超管可见
    ...(user?.role === 'super_admin' ? [{ path: '/users', label: '用户管理' }] : []),
    { path: '/settings', label: '设置' },
  ]

  const handleLogout = async () => {
    await logout()
    navigate('/login', { replace: true })
  }

  const navLinkClass = ({ isActive }: { isActive: boolean }) =>
    `block px-5 py-2.5 text-sm font-medium transition-colors ${
      isActive
        ? 'bg-blue-50 text-blue-700 border-r-2 border-blue-600'
        : 'text-gray-600 hover:text-gray-900 hover:bg-gray-50'
    }`

  return (
    <div className="flex min-h-screen bg-gray-50">
      {/* 移动端顶栏 */}
      <div className="md:hidden fixed top-0 left-0 right-0 z-30 flex items-center gap-3 h-14 px-4 bg-white border-b border-gray-200">
        <button onClick={() => setSidebarOpen(true)} className="text-gray-600 text-xl">☰</button>
        <span className="font-bold text-gray-800">滨湖智慧平台</span>
        {user && <span className="ml-auto text-xs text-gray-400">{ROLE_LABELS[user.role] || user.role}</span>}
      </div>

      {/* 遮罩层 */}
      {sidebarOpen && (
        <div className="md:hidden fixed inset-0 bg-black/30 z-40" onClick={() => setSidebarOpen(false)} />
      )}

      {/* 侧边栏 */}
      <aside className={`fixed md:static inset-y-0 left-0 z-50 w-56 bg-white border-r border-gray-200 flex flex-col transform transition-transform duration-200 md:translate-x-0 ${sidebarOpen ? 'translate-x-0' : '-translate-x-full'}`}>
        <div className="h-14 flex items-center px-5 border-b border-gray-200 shrink-0">
          <h1 className="text-base font-bold text-gray-800">滨湖智慧平台</h1>
        </div>
        <nav className="flex-1 py-4 space-y-1 overflow-y-auto" onClick={() => setSidebarOpen(false)}>
          {menuItems.map((item) => (
            <NavLink key={item.path} to={item.path} end={item.path === '/'} className={navLinkClass}>
              {item.label}
            </NavLink>
          ))}
        </nav>
        {/* 底部用户信息 */}
        {user && (
          <div className="border-t border-gray-200 p-3 shrink-0">
            <div className="text-sm font-medium text-gray-700">{user.username}</div>
            <div className="text-xs text-gray-400 mb-2">{ROLE_LABELS[user.role] || user.role}</div>
            <button onClick={handleLogout} className="text-xs text-red-500 hover:underline">退出登录</button>
          </div>
        )}
      </aside>

      {/* 主内容区 */}
      <main className="flex-1 min-w-0 overflow-auto">
        <div className="p-4 md:p-6 pt-18 md:pt-6">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
