import React, { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { ConfigProvider, theme } from 'antd'
import { AuthProvider } from '../src/context/AuthContext'
import AnalysisImportPanel from '../src/components/AnalysisImportPanel'
import { Panel } from '../src/components/ui'
import { applyThemeToDocument } from '../src/utils/themeMode'
import '../src/index.css'
function Fixture() {
  const [dark, setDark] = useState(false)
  ;(window as any).setFixtureTheme = (value: boolean) => { applyThemeToDocument(value ? 'dark' : 'light'); setDark(value) }
  return <ConfigProvider theme={{ algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm }}><BrowserRouter><AuthProvider><div className="app-page" style={{ padding: 16 }}><Panel title="研判结果工作簿"><AnalysisImportPanel /></Panel></div></AuthProvider></BrowserRouter></ConfigProvider>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
