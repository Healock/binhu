import { lazy, Suspense, type ReactNode } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { AuthProvider, useAuth } from './context/AuthContext'
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
import MobileTaskHome from './pages/MobileTaskHome'
import MobileTaskList from './pages/MobileTaskList'
import MobileTaskDetail from './pages/MobileTaskDetail'
import PoliceAddressManagement from './pages/PoliceAddressManagement'
import PoliceDispatchBatchDetail from './pages/PoliceDispatchBatchDetail'
import PoliceDispatchWorkbench from './pages/PoliceDispatchWorkbench'
import PublicProfile from './pages/PublicProfile'
import RoleDashboard from './pages/RoleDashboard'
import RegistryManagement from './pages/RegistryManagement'
import WatchPeopleManagement from './pages/WatchPeopleManagement'
import WorkflowTickets from './pages/WorkflowTickets'
import AnalysisWorkbench from './pages/AnalysisWorkbench'
import WorkflowConfig from './pages/WorkflowConfig'
import useMobileViewport from './hooks/useMobileViewport'
import {
  canAccessFlowTaskWorkbench,
  shouldUseMobileTaskWorkbench,
  shouldUsePoliceDispatchWorkbench,
} from './utils/mobileTaskRouting'

const DataQuery = lazy(() => import('./pages/DataQuery'))
const TaskFlowLab = lazy(() => import('./pages/TaskFlowLab'))
const ReteTaskFlowLab = lazy(() => import('./pages/ReteTaskFlowLab'))

function LazyPage({ children }: { children: ReactNode }) {
  return (
    <Suspense fallback={<div className="app-card p-10 text-center text-[var(--app-text-secondary)]">正在加载页面…</div>}>
      {children}
    </Suspense>
  )
}

function QueryEntry() {
  const { user } = useAuth()
  const mobile = useMobileViewport()
  if (shouldUsePoliceDispatchWorkbench(user?.member?.position, mobile)) {
    return <Navigate to="/police-tasks" replace />
  }
  if (shouldUseMobileTaskWorkbench(user?.member?.position, mobile)) {
    return <Navigate to="/tasks" replace />
  }
  const permissionGroupCodes = user?.permission_groups?.map(group => group.code) || []
  const adminAccess = permissionGroupCodes.length > 0
    ? permissionGroupCodes.some(code => ['admin', 'super_admin'].includes(code))
    : ['admin', 'super_admin'].includes(user?.role || '')
  return adminAccess
    ? <LazyPage><DataQuery /></LazyPage>
    : <Navigate to="/" replace />
}

function MobileTaskEntry({ detail = false }: { detail?: boolean }) {
  const { user } = useAuth()
  if (!canAccessFlowTaskWorkbench(
    user?.member?.position,
    user?.role,
    user?.permission_groups?.map(group => group.code),
    user?.permissions,
  )) {
    return <Navigate to="/query" replace />
  }
  return detail ? <MobileTaskDetail /> : <MobileTaskList />
}

function MobileTaskHomeEntry() {
  const { user } = useAuth()
  return canAccessFlowTaskWorkbench(
    user?.member?.position,
    user?.role,
    user?.permission_groups?.map(group => group.code),
    user?.permissions,
  )
    ? <MobileTaskHome />
    : <Navigate to="/query" replace />
}

function PhotoTaskEntry() {
  const { user } = useAuth()
  const permissions = new Set(user?.permissions || [])
  const allowed = Boolean(
    user?.member?.position === '基础管控'
    || permissions.has('workflow.ticket.manage')
    || ['admin', 'super_admin'].includes(user?.role || ''),
  )
  return allowed ? <WorkflowTickets mode="photo" /> : <Navigate to="/workflow" replace />
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
              <Route path="/people" element={<Navigate to="/grid-members" replace />} />
              <Route path="/people/:userId" element={<PublicProfile />} />
              <Route path="/" element={<RoleDashboard />} />
              <Route element={<ProtectedRoute requireRole="super_admin" />}>
                <Route path="/task-flow-lab" element={<LazyPage><TaskFlowLab /></LazyPage>} />
                <Route path="/task-flow-rete-lab" element={<LazyPage><ReteTaskFlowLab /></LazyPage>} />
              </Route>
              <Route element={<ProtectedRoute requirePermission="online.summary.view" />}>
                <Route path="/summary" element={<Dashboard />} />
              </Route>
              <Route element={<ProtectedRoute requirePermission="online.raw.view" />}>
                <Route path="/query" element={<QueryEntry />} />
                <Route path="/tasks/home" element={<MobileTaskHomeEntry />} />
                <Route path="/tasks" element={<MobileTaskEntry />} />
                <Route path="/tasks/:parserType/:rowKey" element={<MobileTaskEntry detail />} />
              </Route>
              <Route element={<ProtectedRoute requirePermission="online.task.manage" />}>
                <Route path="/police-analysis/:parserType/:rowKey" element={<MobileTaskDetail mode="analysis" />} />
              </Route>
              <Route element={<ProtectedRoute requirePermission="visit.summary.view" />}>
                <Route path="/visit-summary" element={<VisitSummary />} />
              </Route>
              <Route element={<ProtectedRoute requireAnyPermission={['visit.import', 'police.dispatch.manage', 'workflow.ticket.handle', 'workflow.ticket.manage']} />}>
                <Route path="/data-upload" element={<DataUploadCenter />} />
              </Route>
              <Route element={<ProtectedRoute requirePermission="police.dispatch.manage" />}>
                <Route path="/police-tasks" element={<PoliceDispatchWorkbench />} />
                <Route path="/police-dispatch/batches/:batchId" element={<PoliceDispatchBatchDetail />} />
              </Route>
              <Route element={<ProtectedRoute requireAnyPermission={['online.task.manage', 'police.dispatch.manage']} />}>
                <Route path="/police-analysis" element={<AnalysisWorkbench />} />
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
              <Route element={<ProtectedRoute requirePermission="police.address.manage" />}>
                <Route path="/police-addresses" element={<PoliceAddressManagement />} />
              </Route>
              <Route element={<ProtectedRoute requirePermission="registry.property.view" />}>
                <Route path="/registry" element={<RegistryManagement />} />
              </Route>
              <Route element={<ProtectedRoute requirePermission="registry.watch.view" />}>
                <Route path="/watch-people" element={<WatchPeopleManagement />} />
              </Route>
              <Route element={<ProtectedRoute requirePermission="workflow.ticket.view" />}>
                <Route path="/workflow" element={<WorkflowTickets />} />
              </Route>
              <Route element={<ProtectedRoute requireAnyPermission={['workflow.ticket.handle', 'workflow.ticket.manage']} />}>
                <Route path="/photo-tasks" element={<PhotoTaskEntry />} />
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
                <Route element={<ProtectedRoute requirePermission="workflow.config.manage" />}>
                  <Route path="workflow" element={<WorkflowConfig />} />
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
