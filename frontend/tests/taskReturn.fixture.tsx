import React from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { ConfigProvider, theme } from 'antd'
import { AuthProvider, useAuth } from '../src/context/AuthContext'
import Layout from '../src/components/Layout'
import MobileTaskFlow from '../src/pages/MobileTaskFlow'
import MobileTaskList from '../src/pages/MobileTaskList'
import MobileTaskDetail from '../src/pages/MobileTaskDetail'
import '../src/index.css'

function Session() {
  const { user, loading } = useAuth()
  if (loading || !user) return <div>Loading fixture session</div>
  return <Routes><Route element={<Layout />}>
    {baseline ? <>
      <Route path="/tasks" element={<MobileTaskList />} />
      <Route path="/tasks/:parserType/:rowKey" element={<MobileTaskDetail />} />
    </> : <Route path="/tasks/*" element={<MobileTaskFlow key={user.id} />} />}
    <Route path="/" element={<div>Outside task module</div>} />
  </Route></Routes>
}
const dark = new URLSearchParams(location.search).has('dark')
const baseline = new URLSearchParams(location.search).has('baseline')
document.documentElement.dataset.theme = dark ? 'dark' : 'light'
createRoot(document.getElementById('root')!).render(<ConfigProvider theme={{ algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm }}>
  <BrowserRouter><AuthProvider><Session /></AuthProvider></BrowserRouter>
</ConfigProvider>)
