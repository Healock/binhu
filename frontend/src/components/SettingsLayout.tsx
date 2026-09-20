import { Navigate, NavLink, Outlet, useLocation } from 'react-router-dom'
import {
  BgColorsOutlined,
  CloudDownloadOutlined,
  ClockCircleOutlined,
  DesktopOutlined,
  ApartmentOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons'
import { useAuth } from '../context/AuthContext'
import { PageHeader } from './ui'

export default function SettingsLayout() {
  const { user } = useAuth()
  const location = useLocation()
  const allMenuItems = [
    { path: '/settings/system', label: '系统设置', icon: <ClockCircleOutlined />, permission: 'system.manage' },
    { path: '/settings/server-mac', label: '服务器 MAC', icon: <DesktopOutlined />, permission: 'system.server_mac.manage' },
    { path: '/settings/workflow', label: '工单流程配置', icon: <ApartmentOutlined />, permission: 'workflow.config.manage' },
    { path: '/settings/account-security', label: '账号与安全', icon: <SafetyCertificateOutlined />, permission: '' },
    { path: '/settings/personalization', label: '个性化', icon: <BgColorsOutlined />, permission: '' },
    { path: '/settings/updates', label: '应用更新', icon: <CloudDownloadOutlined />, permission: '' },
  ]

  const menuItems = allMenuItems.filter(item => !item.permission || user?.permissions?.includes(item.permission))
  const restrictedPaths = allMenuItems.filter(item => item.permission && !user?.permissions?.includes(item.permission)).map(item => item.path)

  if (restrictedPaths.some(path => location.pathname.startsWith(path))) {
    return <Navigate to="/settings/personalization" replace />
  }

  const linkClass = ({ isActive }: { isActive: boolean }) =>
    `flex min-h-10 items-center gap-2.5 rounded-lg px-3 text-sm font-medium transition-colors ${
      isActive
        ? 'bg-blue-50 text-blue-700'
        : 'text-slate-600 hover:bg-slate-50 hover:text-slate-900'
    }`

  return (
    <div className="app-page min-w-0">
      <PageHeader title="系统设置" description="管理平台运行参数和个人显示习惯" />

      <div className="lg:flex lg:items-start lg:gap-6">
        <aside className="hidden w-52 shrink-0 lg:block">
          <nav className="app-card space-y-1 p-2">
            {menuItems.map(item => (
              <NavLink key={item.path} to={item.path} className={linkClass}>
                <span className="flex w-5 justify-center">{item.icon}</span>
                {item.label}
              </NavLink>
            ))}
          </nav>
        </aside>

        <nav className="mb-4 flex w-full max-w-full gap-2 overflow-x-auto pb-1 lg:hidden">
          {menuItems.map(item => (
            <NavLink key={item.path} to={item.path} className={({ isActive }) => `${linkClass({ isActive })} shrink-0`}>
              {item.icon}
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="settings-content min-w-0 max-w-full flex-1">
          <Outlet />
        </div>
      </div>
    </div>
  )
}
