import { Link, Navigate, Outlet } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import type { PermissionCode, Role } from '../types'

interface Props {
  requireRole?: Role
  requireRoles?: Role[]
  requirePermission?: PermissionCode
  requireAnyPermission?: PermissionCode[]
}

export default function ProtectedRoute({ requireRole, requireRoles, requirePermission, requireAnyPermission }: Props) {
  const { user, loading } = useAuth()

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <span className="text-gray-400 text-sm">加载中...</span>
      </div>
    )
  }

  if (!user) {
    return <Navigate to="/login" replace />
  }

  if (
    (requireRole && user.role !== requireRole)
    || (requireRoles && !requireRoles.includes(user.role))
  ) {
    return <AccessDenied title="当前账号没有访问该页面的岗位权限。" />
  }
  if (requirePermission && !user.permissions?.includes(requirePermission)) {
    return <AccessDenied title={`当前账号缺少功能权限：${requirePermission}`} />
  }
  if (
    requireAnyPermission?.length
    && !requireAnyPermission.some(permission => user.permissions?.includes(permission))
  ) {
    return <AccessDenied title="当前账号没有满足该页面要求的功能权限。" />
  }

  return <Outlet />
}

function AccessDenied({ title }: { title: string }) {
  return (
    <div className="flex min-h-[60vh] items-center justify-center px-4">
      <div className="w-full max-w-md rounded-xl border border-amber-200 bg-amber-50 p-6 text-center shadow-sm dark:border-amber-900/60 dark:bg-amber-950/20">
        <div className="text-lg font-semibold text-amber-900 dark:text-amber-200">无权访问</div>
        <p className="mt-2 text-sm text-amber-800 dark:text-amber-300">{title}</p>
        <p className="mt-2 text-xs text-amber-700/80 dark:text-amber-300/80">如需开通，请联系管理员，并说明你要访问的页面。</p>
        <div className="mt-5 flex justify-center gap-2">
          <button type="button" className="rounded-md border border-amber-300 px-3 py-1.5 text-sm text-amber-900 hover:bg-amber-100 dark:border-amber-700 dark:text-amber-200 dark:hover:bg-amber-900/30" onClick={() => window.history.back()}>返回上一页</button>
          <Link className="rounded-md bg-amber-700 px-3 py-1.5 text-sm text-white hover:bg-amber-800" to="/">返回首页</Link>
        </div>
      </div>
    </div>
  )
}
