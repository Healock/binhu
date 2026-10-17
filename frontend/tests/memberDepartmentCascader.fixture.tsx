import React, { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { ConfigProvider, Modal, theme } from 'antd'
import MemberDepartmentCascader from '../src/components/MemberDepartmentCascader'
import type { DepartmentOption } from '../src/api/client'
import '../src/index.css'

const departments: DepartmentOption[] = [
  { id: 1, name: '测试单位', type: 'unit', parent_id: null, area_id: null, community_name: null, is_active: true },
  { id: 2, name: '测试东片', type: 'unit', parent_id: 1, area_id: 1, community_name: null, is_active: true },
  { id: 3, name: '测试甲社区', type: 'community', parent_id: 2, area_id: 1, community_name: '测试甲社区', is_active: true },
  { id: 4, name: '测试乙社区', type: 'community', parent_id: 2, area_id: 1, community_name: '测试乙社区', is_active: true },
  { id: 5, name: '测试西片', type: 'unit', parent_id: 1, area_id: 2, community_name: null, is_active: true },
  { id: 6, name: '测试丙社区', type: 'community', parent_id: 5, area_id: 2, community_name: '测试丙社区', is_active: true },
]

function Fixture() {
  const [mode, setMode] = useState('multiple')
  const [ids, setIds] = useState<number[]>([])
  const dark = new URLSearchParams(location.search).has('dark')
  return <ConfigProvider theme={{ algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm }}>
    <Modal open title="人员部门选择（合成测试）" width={580} footer={null}>
      <div style={{ display: 'grid', gap: 16 }}>
        <div style={{ display: 'flex', gap: 8 }}>
          {['multiple', 'community', 'unit'].map(value => <button key={value} onClick={() => {
            setMode(value); setIds([])
          }}>{value}</button>)}
        </div>
        <MemberDepartmentCascader departments={departments} value={ids} onChange={setIds}
          communityPosition={mode !== 'unit'} multiple={mode === 'multiple'} />
        <output data-testid="department-ids">{JSON.stringify(ids)}</output>
      </div>
    </Modal>
  </ConfigProvider>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
