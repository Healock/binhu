import { useState, useEffect, useCallback } from 'react'
import { Alert, Button, DatePicker, Input, Modal, Pagination, Select, Tag } from 'antd'
import { DownloadOutlined, PlusOutlined, SearchOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'
import {
  listGridMembers, createGridMember, updateGridMember, deleteGridMember,
  exportGridMembersUrl, getGridCommunities,
  type GridMember,
} from '../api/client'
import { EmptyState, LoadingState, PageHeader } from '../components/ui'

interface Community { id: number; name: string; grid_count: number }

export default function GridMembers() {
  const [members, setMembers] = useState<GridMember[]>([])
  const [communities, setCommunities] = useState<Community[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [keyword, setKeyword] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [communityFilter, setCommunityFilter] = useState('')
  const [loading, setLoading] = useState(false)
  const [editing, setEditing] = useState<GridMember | null>(null)
  const [showAddForm, setShowAddForm] = useState(false)
  const [msg, setMsg] = useState('')
  const [loadError, setLoadError] = useState('')
  const pageSize = 100

  const fetch = useCallback(async () => {
    setLoading(true)
    setLoadError('')
    try {
      const res = await listGridMembers({ keyword: keyword || undefined, community: communityFilter || undefined, page, page_size: pageSize })
      setMembers(res.data); setTotal(res.total)
    } catch {
      setLoadError('网格员列表加载失败，请稍后重试')
    } finally { setLoading(false) }
  }, [keyword, communityFilter, page, pageSize])

  const fetchCommunities = useCallback(async () => {
    try { setCommunities(await getGridCommunities()) } catch {}
  }, [])

  useEffect(() => { fetch() }, [fetch])
  useEffect(() => { fetchCommunities() }, [fetchCommunities, members])

  const handleDelete = (id: number, name: string) => {
    Modal.confirm({
      title: '删除网格员',
      content: `确认删除网格员“${name}”？`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await deleteGridMember(id)
          setMsg(`已删除网格员“${name}”`)
          fetch()
        } catch {
          setMsg('删除失败，请稍后重试')
        }
      },
    })
  }

  const handleExport = () => { window.open(exportGridMembersUrl(), '_blank') }

  const communityNames = communities.map((c) => c.name)
  const activeCount = members.filter(m => m.effective_status === '在岗').length

  return (
    <div className="app-page">
      <PageHeader
        title="网格员管理"
        description="维护长期状态和请假日期，请假期间会自动显示为离岗"
        actions={
          <>
            <Button icon={<DownloadOutlined />} onClick={handleExport}>导出 CSV</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setShowAddForm(true)}>添加网格员</Button>
          </>
        }
      />

      <section className="app-card">
        <div className="app-toolbar">
          <Input
            allowClear
            prefix={<SearchOutlined className="text-slate-400" />}
            placeholder="搜索姓名或电话"
            value={searchInput}
            onChange={event => setSearchInput(event.target.value)}
            onPressEnter={() => { setKeyword(searchInput); setPage(1) }}
            className="min-w-56 flex-1"
          />
          <Select
            value={communityFilter}
            onChange={value => { setCommunityFilter(value); setPage(1) }}
            className="min-w-40"
            options={[
              { value: '', label: '全部社区' },
              ...communityNames.map(community => ({ value: community, label: community })),
            ]}
          />
          <Button type="primary" icon={<SearchOutlined />} onClick={() => { setKeyword(searchInput); setPage(1) }}>
            搜索
          </Button>
          <div className="ml-auto flex gap-2">
            <Tag color="blue">共 {total} 人</Tag>
            <Tag color="green">当前页在岗 {activeCount} 人</Tag>
          </div>
        </div>
        {msg && <Alert type={msg.includes('失败') ? 'error' : 'success'} showIcon message={msg} />}
      </section>

      <div className="app-table-wrap">
        {loading ? <LoadingState /> :
         loadError ? <EmptyState label={loadError} /> :
         members.length === 0 ? <EmptyState label="暂无网格员，可点击“添加网格员”手动添加" /> :
         <table className="app-table min-w-full">
           <thead className="bg-gray-50 border-b"><tr>
             <th className="px-3 py-2 text-left font-medium text-gray-600">姓名</th>
             <th className="px-3 py-2 text-left font-medium text-gray-600">所属社区</th>
             <th className="px-3 py-2 text-left font-medium text-gray-600">电话</th>
             <th className="px-3 py-2 text-left font-medium text-gray-600">状态</th>
             <th className="px-3 py-2 text-left font-medium text-gray-600">备注</th>
             <th className="px-3 py-2 text-left font-medium text-gray-600">操作</th>
           </tr></thead>
           <tbody className="divide-y divide-gray-100">
             {members.map((m) => (
               <tr key={m.id} className={`hover:bg-gray-50 ${m.effective_status === '离岗' ? 'bg-slate-50/60' : ''}`}>
                 <td className="px-3 py-2 font-medium text-gray-800">{m.name}</td>
                 <td className="px-3 py-2 text-gray-600">{m.community || '-'}</td>
                 <td className="px-3 py-2 text-gray-600">{m.phone || '-'}</td>
                 <td className="px-3 py-2">
                   <MemberStatus member={m} />
                 </td>
                 <td className="px-3 py-2 text-gray-600">{m.notes || '-'}</td>
                 <td className="px-3 py-2">
                   <Button type="link" size="small" onClick={() => setEditing(m)}>编辑</Button>
                   <Button type="link" danger size="small" onClick={() => handleDelete(m.id, m.name)}>删除</Button>
                 </td>
               </tr>
             ))}
           </tbody>
         </table>}
      </div>

      {total > pageSize && (
        <div className="flex justify-center">
          <Pagination current={page} pageSize={pageSize} total={total} showSizeChanger={false} onChange={setPage} />
        </div>
      )}

      {(showAddForm || editing) && (
        <MemberForm member={editing} communities={communityNames}
          onClose={() => { setShowAddForm(false); setEditing(null) }}
          onSaved={() => {
            setShowAddForm(false)
            setEditing(null)
            setMsg(editing ? '网格员信息已更新' : '网格员已添加')
            fetch()
          }} />
      )}
    </div>
  )
}

function MemberStatus({ member }: { member: GridMember }) {
  const isLeave = member.status === '在岗' && member.effective_status === '离岗'
  const isPermanentOffDuty = member.status === '离岗'
  const label = isLeave ? '离岗（请假）' : isPermanentOffDuty ? '长期离岗' : '在岗'
  const color = isLeave ? 'orange' : isPermanentOffDuty ? 'default' : 'green'
  const detail = isPermanentOffDuty ? '' : member.status_detail

  return (
    <div className="min-w-32">
      <Tag color={color}>{label}</Tag>
      {detail && <div className="mt-1 text-xs text-slate-500">{detail}</div>}
      {member.leave_reason && (isLeave || member.leave_state === 'upcoming') && (
        <div className="mt-0.5 max-w-48 truncate text-xs text-slate-400" title={member.leave_reason}>
          {member.leave_reason}
        </div>
      )}
    </div>
  )
}

function MemberForm({ member, communities, onClose, onSaved }: {
  member: GridMember | null; communities: string[]; onClose: () => void; onSaved: () => void
}) {
  const [name, setName] = useState(member?.name || '')
  const [community, setCommunity] = useState(member?.community || '')
  const [phone, setPhone] = useState(member?.phone || '')
  const [notes, setNotes] = useState(member?.notes || '')
  const [status, setStatus] = useState<'在岗' | '离岗'>(
    member?.status === '离岗' ? '离岗' : '在岗'
  )
  const [leaveRange, setLeaveRange] = useState<[string, string] | null>(
    member?.leave_start_date && member?.leave_end_date
      ? [member.leave_start_date, member.leave_end_date]
      : null
  )
  const [leaveReason, setLeaveReason] = useState(member?.leave_reason || '')
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')

  const handleSave = async () => {
    setSaving(true)
    setFormError('')
    try {
      const originalRange = member?.leave_start_date && member?.leave_end_date
        ? [member.leave_start_date, member.leave_end_date]
        : null
      const leaveChanged =
        JSON.stringify(originalRange) !== JSON.stringify(leaveRange) ||
        (member?.leave_reason || '') !== leaveReason
      const payload = {
        community,
        phone,
        notes,
        status,
        leave_start_date: leaveRange?.[0] || null,
        leave_end_date: leaveRange?.[1] || null,
        leave_reason: leaveRange ? leaveReason : '',
        leave_source: leaveChanged ? 'manual' : (member?.leave_source || 'manual'),
      }
      if (member) await updateGridMember(member.id, payload)
      else await createGridMember({ name, ...payload })
      onSaved()
    } catch (e: any) { setFormError(e?.response?.data?.detail || '保存失败') }
    finally { setSaving(false) }
  }

  return (
    <Modal
      open
      title={member ? '编辑网格员' : '添加网格员'}
      okText="保存"
      cancelText="取消"
      confirmLoading={saving}
      maskClosable={!saving}
      onOk={handleSave}
      onCancel={onClose}
    >
        <div className="space-y-4 pt-2">
          <div>
            <label className="mb-1.5 block text-sm font-medium text-slate-700">姓名</label>
            <Input value={name} onChange={event => setName(event.target.value)} disabled={!!member} />
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium text-slate-700">所属社区</label>
            <Select
              value={community || undefined}
              onChange={setCommunity}
              placeholder="请选择社区"
              className="w-full"
              options={communities.map(item => ({ value: item, label: item }))}
            />
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium text-slate-700">电话</label>
            <Input value={phone} onChange={event => setPhone(event.target.value)} />
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium text-slate-700">长期状态</label>
            <Select
              value={status}
              onChange={setStatus}
              className="w-full"
              options={[
                { value: '在岗', label: '正常在岗' },
                { value: '离岗', label: '长期离岗' },
              ]}
            />
            <p className="mt-1.5 text-xs text-slate-500">长期离岗适合调离或不再参与工作的人员。</p>
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium text-slate-700">临时请假日期</label>
            <DatePicker.RangePicker
              value={leaveRange ? [dayjs(leaveRange[0]), dayjs(leaveRange[1])] : null}
              onChange={(_, dateStrings) => {
                setLeaveRange(
                  dateStrings[0] && dateStrings[1]
                    ? [dateStrings[0], dateStrings[1]]
                    : null
                )
              }}
              format="YYYY-MM-DD"
              placeholder={['开始日期', '结束日期']}
              className="w-full"
            />
            <p className="mt-1.5 text-xs text-slate-500">
              日期范围内自动显示“离岗（请假）”，结束后自动恢复在岗。
            </p>
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium text-slate-700">请假原因</label>
            <Input
              value={leaveReason}
              onChange={event => setLeaveReason(event.target.value)}
              disabled={!leaveRange}
              maxLength={200}
              placeholder={leaveRange ? '例如：年假、病假' : '请先选择请假日期'}
            />
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium text-slate-700">备注</label>
            <Input.TextArea value={notes} onChange={event => setNotes(event.target.value)} rows={3} />
          </div>
          {formError && <p className="text-sm text-red-700">{formError}</p>}
        </div>
    </Modal>
  )
}
