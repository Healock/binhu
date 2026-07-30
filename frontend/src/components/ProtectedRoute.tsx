import { Navigate, Outlet } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import type { Role } from '../types'

interface Props {
  requireRole?: Role
  requireRoles?: Role[]
}

export default function ProtectedRoute({ requireRole, requireRoles }: Props) {
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

  return <Outlet />
}
