import { Navigate, Outlet } from 'react-router-dom'
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
    return <Navigate to="/" replace />
  }
  if (requirePermission && !user.permissions?.includes(requirePermission)) {
    return <Navigate to="/settings/personalization" replace />
  }
  if (
    requireAnyPermission?.length
    && !requireAnyPermission.some(permission => user.permissions?.includes(permission))
  ) {
    return <Navigate to="/settings/personalization" replace />
  }

  return <Outlet />
}
