import React, { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Button, ConfigProvider, theme } from 'antd'
import MobileTaskAssignmentWorkbench from '../src/components/MobileTaskAssignmentWorkbench'
import { applyThemeToDocument } from '../src/utils/themeMode'
import '../src/index.css'

function Fixture() {
  const [open, setOpen] = useState(false)
  const [dark, setDark] = useState(false)
  ;(window as any).setFixtureTheme = (value: boolean) => {
    applyThemeToDocument(value ? 'dark' : 'light'); setDark(value)
  }
  return <ConfigProvider theme={{ algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm }}>
    <Button onClick={() => setOpen(true)}>打开虚构分配工作台</Button>
    <MobileTaskAssignmentWorkbench open={open} parserType="全链条" onClose={() => setOpen(false)} onChanged={() => {}} />
  </ConfigProvider>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
