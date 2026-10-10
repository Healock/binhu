import React from 'react'
import { createRoot } from 'react-dom/client'
import { ConfigProvider, theme } from 'antd'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { AuthProvider } from '../src/context/AuthContext'
import MobileTaskDetail from '../src/pages/MobileTaskDetail'
import '../src/index.css'

const dark = new URLSearchParams(location.search).has('dark')
document.documentElement.dataset.theme = dark ? 'dark' : 'light'
createRoot(document.getElementById('root')!).render(
  <AuthProvider>
    <MemoryRouter initialEntries={['/tasks/全链条/fixture-row']}>
      <ConfigProvider theme={{ algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm }}>
        <Routes><Route path="/tasks/:parserType/:rowKey" element={<MobileTaskDetail />} /></Routes>
      </ConfigProvider>
    </MemoryRouter>
  </AuthProvider>,
)
