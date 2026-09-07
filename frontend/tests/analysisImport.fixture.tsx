import React, { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { ConfigProvider, theme } from 'antd'
import { AuthProvider } from '../src/context/AuthContext'
import MobileTaskList from '../src/pages/MobileTaskList'
import { applyThemeToDocument } from '../src/utils/themeMode'
import '../src/index.css'
function Fixture() {
  const [dark, setDark] = useState(false)
  ;(window as any).setFixtureTheme = (value: boolean) => { applyThemeToDocument(value ? 'dark' : 'light'); setDark(value) }
  return <ConfigProvider theme={{ algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm }}><BrowserRouter><AuthProvider><div style={{ padding: 16 }}><MobileTaskList mode="analysis" /></div></AuthProvider></BrowserRouter></ConfigProvider>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
