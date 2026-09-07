import React, { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { ConfigProvider, theme } from 'antd'
import { AuthProvider } from '../src/context/AuthContext'
import AddressConfirmation from '../src/pages/AddressConfirmation'
import '../src/index.css'
import { applyThemeToDocument } from '../src/utils/themeMode'
function Fixture() {
  const [dark, setDark] = useState(false)
  ;(window as any).setFixtureTheme = (value: boolean) => { applyThemeToDocument(value ? 'dark' : 'light'); setDark(value) }
  return <ConfigProvider theme={{algorithm:dark?theme.darkAlgorithm:theme.defaultAlgorithm}}><BrowserRouter><AuthProvider><div style={{padding:16,maxWidth:1800,margin:'0 auto'}}><AddressConfirmation /></div></AuthProvider></BrowserRouter></ConfigProvider>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
