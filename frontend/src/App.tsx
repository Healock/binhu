import { lazy, Suspense, type ReactNode } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { AuthProvider } from './context/AuthContext'
import AppThemeProvider from './components/AppThemeProvider'
import ProtectedRoute from './components/ProtectedRoute'
import Layout from './components/Layout'
import SettingsLayout from './components/SettingsLayout'
import Dashboard from './pages/Dashboard'
import GridMembers from './pages/GridMembers'
import WeekendDuty from './pages/WeekendDuty'
import Communities from './pages/Communities'
import UserManagement from './pages/UserManagement'
import SpreadsheetSettings from './pages/SpreadsheetSettings'
import OAuthSettings from './pages/OAuthSettings'
import SystemSettings from './pages/SystemSettings'
import PersonalizationSettings from './pages/PersonalizationSettings'
import OperationsCenter from './pages/OperationsCenter'
import VisitSummary from './pages/VisitSummary'
import DataUploadCenter from './pages/DataUploadCenter'
import Login from './pages/Login'
import WorkLog from './pages/WorkLog'
import WorkLogDrafts from './pages/WorkLogDrafts'
import PermissionGroups from './pages/PermissionGroups'
import Profile from './pages/Profile'

const DataQuery = lazy(() => import('./pages/DataQuery'))

function LazyPage({ children }: { children: ReactNode }) {
  return (
    <Suspense fallback={<div className="app-card p-10 text-center text-[var(--app-text-secondary)]">正在加载在线工作表…</div>}>
      {children}
    </Suspense>
  )
}

function App() {
  return (
    <AuthProvider>
      <AppThemeProvider>
        <Routes>
          {/* 登录页不套 Layout */}
          <Route path="/login" element={<Login />} />

          {/* 其他页面需要登录 */}
          <Route element={<ProtectedRoute />}>
            <Route element={<Layout />}>
              <Route path="/profile" element={<Profile />} />
              <Route element={<ProtectedRoute requirePermission="online.summary.view" />}>
                <Route path="/" element={<Dashboard />} />
              </Route>
              <Route element={<ProtectedRoute requirePermission="online.raw.view" />}>
                <Route path="/query" element={<LazyPage><DataQuery /></LazyPage>} />
              </Route>
              <Route element={<ProtectedRoute requirePermission="visit.summary.view" />}>
                <Route path="/visit-summary" element={<VisitSummary />} />
              </Route>
              <Route element={<ProtectedRoute requirePermission="visit.import" />}>
                <Route path="/data-upload" element={<DataUploadCenter />} />
              </Route>
              <Route element={<ProtectedRoute requirePermission="worklog.manage" />}>
                <Route path="/work-log" element={<WorkLog />} />
                <Route path="/work-log/drafts" element={<WorkLogDrafts />} />
              </Route>
              <Route element={<ProtectedRoute requirePermission="personnel.basic.view" />}>
                <Route path="/grid-members" element={<GridMembers />} />
              </Route>
              <Route element={<ProtectedRoute requirePermission="attendance.manage" />}>
                <Route path="/grid-members/weekend-duty" element={<WeekendDuty />} />
              </Route>
              <Route element={<ProtectedRoute requirePermission="community.view" />}>
                <Route path="/communities" element={<Communities />} />
              </Route>

              {/* 用户管理仅超管 */}
              <Route element={<ProtectedRoute requirePermission="user.manage" />}>
                <Route path="/users" element={<UserManagement />} />
              </Route>
              <Route element={<ProtectedRoute requirePermission="permission.manage" />}>
                <Route path="/permission-groups" element={<PermissionGroups />} />
              </Route>
              <Route element={<ProtectedRoute requirePermission="ops.manage" />}>
                <Route path="/operations" element={<OperationsCenter />} />
                <Route path="/settings/operations" element={<Navigate to="/operations" replace />} />
              </Route>

              <Route path="/settings" element={<SettingsLayout />}>
                <Route index element={<Navigate to="/settings/personalization" replace />} />
                <Route path="personalization" element={<PersonalizationSettings />} />
                <Route element={<ProtectedRoute requirePermission="system.manage" />}>
                  <Route path="spreadsheets" element={<SpreadsheetSettings />} />
                  <Route path="oauth" element={<OAuthSettings />} />
                  <Route path="system" element={<SystemSettings />} />
                </Route>
              </Route>
            </Route>
          </Route>

          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </AppThemeProvider>
    </AuthProvider>
  )
}

export default App
