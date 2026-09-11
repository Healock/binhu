import React, { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { ConfigProvider, theme } from 'antd'
import { BrowserRouter } from 'react-router-dom'
import MobileTaskTable from '../src/components/MobileTaskTable'
import '../src/index.css'
const task: any = { task_key: '全链条:fixture:1', row_key: 'fixture:1', parser_type: '全链条',
  summary: { title: '虚构输入任务', original_address: '虚构测试地址', current_address: '', result: '无法核实', secondary_feedback: '', analysis: '', phone: '', identity_number: '', deadline: '2026-09-13' },
  state: 'checked', community: '虚构社区', inspector: '虚构核查员', needs_review: false, source_count: 1, conflict: false, pending_sync: false, priority: 'ordinary', watch_marks: [], sync_state: 'synced' }
function Fixture() {
 const [rows, setRows] = useState([task]); const [active, setActive] = useState(true)
 ;(window as any).refreshFixture = () => setRows([{ ...task }])
 return <><button onClick={() => setActive(!active)}>切换详情</button><div style={{ display: active ? '' : 'none' }}>
 <MobileTaskTable active={active} rows={rows} loading={false} selectionMode={false} selectedRowKeys={[]} canSelect={() => true} onSelect={() => {}} onOpen={() => setActive(false)} onAddressOpen={() => {}} onCopy={() => {}} sort={{ field: 'deadline', order: 'asc' } as any} onSortChange={() => {}} onSaved={() => {}} />
 </div></>
}
const dark = new URLSearchParams(location.search).has('dark'); document.documentElement.dataset.theme = dark ? 'dark' : 'light'
createRoot(document.getElementById('root')!).render(<BrowserRouter><ConfigProvider theme={{ algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm }}><Fixture /></ConfigProvider></BrowserRouter>)
