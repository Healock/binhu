import { useState, useEffect, useCallback } from 'react'
import { Alert, Button, Input, Modal, Pagination, Select, Tag } from 'antd'
import { DownloadOutlined, PlusOutlined, SearchOutlined } from '@ant-design/icons'
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

  const handleToggleStatus = async (m: GridMember) => {
    const newStatus = m.status === '在岗' ? '离岗' : '在岗'
    try {
      await updateGridMember(m.id, { status: newStatus })
      setMembers(prev => prev.map(x => x.id === m.id ? { ...x, status: newStatus } : x))
      setMsg(`已将“${m.name}”设为${newStatus}`)
    } catch {
      setMsg('状态更新失败，请稍后重试')
    }
  }

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
  const activeCount = members.filter(m => m.status === '在岗').length

  return (
    <div className="app-page">
      <PageHeader
        title="网格员管理"
        description="维护网格员、所属社区、联系方式和在岗状态"
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
               <tr key={m.id} className={`hover:bg-gray-50 ${m.status === '离岗' ? 'opacity-50' : ''}`}>
                 <td className="px-3 py-2 font-medium text-gray-800">{m.name}</td>
                 <td className="px-3 py-2 text-gray-600">{m.community || '-'}</td>
                 <td className="px-3 py-2 text-gray-600">{m.phone || '-'}</td>
                 <td className="px-3 py-2">
                   <button onClick={() => handleToggleStatus(m)}
                     aria-label={`将${m.name}设为${m.status === '在岗' ? '离岗' : '在岗'}`}
                     className={`compact-action rounded px-2 py-1 text-xs font-medium ${
                       m.status === '在岗'
                         ? 'bg-green-100 text-green-700 hover:bg-green-200'
                         : 'bg-gray-100 text-gray-500 hover:bg-gray-200'
                     }`}>
                     {m.status}
                   </button>
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

function MemberForm({ member, communities, onClose, onSaved }: {
  member: GridMember | null; communities: string[]; onClose: () => void; onSaved: () => void
}) {
  const [name, setName] = useState(member?.name || '')
  const [community, setCommunity] = useState(member?.community || '')
  const [phone, setPhone] = useState(member?.phone || '')
  const [notes, setNotes] = useState(member?.notes || '')
  const [status, setStatus] = useState(member?.status || '在岗')
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')

  const handleSave = async () => {
    setSaving(true)
    setFormError('')
    try {
      if (member) await updateGridMember(member.id, { community, phone, notes, status })
      else await createGridMember({ name, community, phone, notes, status })
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
            <label className="mb-1.5 block text-sm font-medium text-slate-700">状态</label>
            <Select
              value={status}
              onChange={setStatus}
              className="w-full"
              options={[{ value: '在岗', label: '在岗' }, { value: '离岗', label: '离岗' }]}
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
