import { Routes, Route, Navigate } from 'react-router-dom'
import { AuthProvider } from './context/AuthContext'
import ProtectedRoute from './components/ProtectedRoute'
import Layout from './components/Layout'
import SettingsLayout from './components/SettingsLayout'
import Dashboard from './pages/Dashboard'
import DataQuery from './pages/DataQuery'
import GridMembers from './pages/GridMembers'
import Communities from './pages/Communities'
import UserManagement from './pages/UserManagement'
import SpreadsheetSettings from './pages/SpreadsheetSettings'
import OAuthSettings from './pages/OAuthSettings'
import SystemSettings from './pages/SystemSettings'
import PersonalizationSettings from './pages/PersonalizationSettings'
import OperationsCenter from './pages/OperationsCenter'
import VisitSummary from './pages/VisitSummary'
import Login from './pages/Login'

function App() {
  return (
    <AuthProvider>
      <Routes>
        {/* 登录页不套 Layout */}
        <Route path="/login" element={<Login />} />

        {/* 其他页面需要登录 */}
        <Route element={<ProtectedRoute />}>
          <Route element={<Layout />}>
            <Route path="/" element={<Dashboard />} />
            <Route path="/query" element={<DataQuery />} />
            <Route path="/visit-summary" element={<VisitSummary />} />
            <Route path="/grid-members" element={<GridMembers />} />
            <Route path="/communities" element={<Communities />} />

            {/* 用户管理仅超管 */}
            <Route element={<ProtectedRoute requireRole="super_admin" />}>
              <Route path="/users" element={<UserManagement />} />
              <Route path="/operations" element={<OperationsCenter />} />
              <Route path="/settings/operations" element={<Navigate to="/operations" replace />} />
            </Route>

            <Route path="/settings" element={<SettingsLayout />}>
              <Route index element={<Navigate to="/settings/personalization" replace />} />
              <Route path="spreadsheets" element={<SpreadsheetSettings />} />
              <Route path="oauth" element={<OAuthSettings />} />
              <Route path="system" element={<SystemSettings />} />
              <Route path="personalization" element={<PersonalizationSettings />} />
            </Route>
          </Route>
        </Route>

        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AuthProvider>
  )
}

export default App
