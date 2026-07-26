import { useState, useEffect, useCallback } from 'react'
import {
  listGridMembers, createGridMember, updateGridMember, deleteGridMember,
  exportGridMembersUrl, getGridCommunities,
  type GridMember,
} from '../api/client'

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

  const fetch = useCallback(async () => {
    setLoading(true)
    try {
      const res = await listGridMembers({ keyword: keyword || undefined, community: communityFilter || undefined, page, page_size: 100 })
      setMembers(res.data); setTotal(res.total)
    } finally { setLoading(false) }
  }, [keyword, communityFilter, page])

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
    } catch {}
  }

  const handleDelete = async (id: number, name: string) => {
    if (!confirm(`确认删除网格员"${name}"？`)) return
    try { await deleteGridMember(id); fetch() } catch {}
  }

  const handleExport = () => { window.open(exportGridMembersUrl(), '_blank') }

  const communityNames = communities.map((c) => c.name)
  const activeCount = members.filter(m => m.status === '在岗').length

  return (
    <div className="space-y-4">
      <div className="bg-white rounded-lg shadow p-4">
        <div className="flex flex-wrap gap-3 items-center">
          <input placeholder="搜索姓名/电话..." value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && (setKeyword(searchInput), setPage(1))}
            className="border border-gray-300 rounded px-3 py-1.5 text-sm flex-1 min-w-32" />
          <select value={communityFilter} onChange={(e) => (setCommunityFilter(e.target.value), setPage(1))}
            className="border border-gray-300 rounded px-3 py-1.5 text-sm">
            <option value="">全部社区</option>
            {communityNames.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
          <button onClick={handleExport} className="px-3 py-1.5 border border-gray-300 rounded text-sm hover:bg-gray-50">导出CSV</button>
          <button onClick={() => setShowAddForm(true)} className="px-3 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700">添加</button>
          <span className="text-sm text-gray-500 ml-auto">共 {total} 人 · 在岗 {activeCount} 人</span>
        </div>
        {msg && <p className="text-sm text-gray-500 mt-2">{msg}</p>}
      </div>

      <div className="bg-white rounded-lg shadow overflow-auto">
        {loading ? <p className="p-8 text-center text-gray-400 text-sm">加载中...</p> :
         members.length === 0 ? <p className="p-8 text-center text-gray-400 text-sm">暂无网格员，点击"提取网格员"自动提取</p> :
         <table className="min-w-full text-sm">
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
                     className={`px-2 py-0.5 rounded text-xs font-medium ${
                       m.status === '在岗'
                         ? 'bg-green-100 text-green-700 hover:bg-green-200'
                         : 'bg-gray-100 text-gray-500 hover:bg-gray-200'
                     }`}>
                     {m.status}
                   </button>
                 </td>
                 <td className="px-3 py-2 text-gray-600">{m.notes || '-'}</td>
                 <td className="px-3 py-2">
                   <button onClick={() => setEditing(m)} className="text-blue-500 hover:underline text-xs mr-2">编辑</button>
                   <button onClick={() => handleDelete(m.id, m.name)} className="text-red-500 hover:underline text-xs">删除</button>
                 </td>
               </tr>
             ))}
           </tbody>
         </table>}
      </div>

      {(showAddForm || editing) && (
        <MemberForm member={editing} communities={communityNames}
          onClose={() => { setShowAddForm(false); setEditing(null) }}
          onSaved={() => { setShowAddForm(false); setEditing(null); fetch() }} />
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

  const handleSave = async () => {
    setSaving(true)
    try {
      if (member) await updateGridMember(member.id, { community, phone, notes, status })
      else await createGridMember({ name, community, phone, notes, status })
      onSaved()
    } catch (e: any) { alert(e?.response?.data?.detail || '保存失败') }
    finally { setSaving(false) }
  }

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-white rounded-lg shadow-xl p-6 w-full max-w-md mx-4" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-base font-semibold text-gray-800 mb-4">{member ? '编辑网格员' : '添加网格员'}</h3>
        <div className="space-y-3">
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">姓名</label>
            <input value={name} onChange={(e) => setName(e.target.value)} disabled={!!member}
              className="w-full border border-gray-300 rounded px-3 py-1.5 text-sm disabled:bg-gray-100" />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">所属社区</label>
            <select value={community} onChange={(e) => setCommunity(e.target.value)}
              className="w-full border border-gray-300 rounded px-3 py-1.5 text-sm">
              <option value="">请选择社区</option>
              {communities.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">电话</label>
            <input value={phone} onChange={(e) => setPhone(e.target.value)}
              className="w-full border border-gray-300 rounded px-3 py-1.5 text-sm" />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">状态</label>
            <select value={status} onChange={(e) => setStatus(e.target.value)}
              className="w-full border border-gray-300 rounded px-3 py-1.5 text-sm">
              <option value="在岗">在岗</option>
              <option value="离岗">离岗</option>
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">备注</label>
            <input value={notes} onChange={(e) => setNotes(e.target.value)}
              className="w-full border border-gray-300 rounded px-3 py-1.5 text-sm" />
          </div>
        </div>
        <div className="flex justify-end gap-2 mt-5">
          <button onClick={onClose} className="px-4 py-1.5 border border-gray-300 rounded text-sm hover:bg-gray-50">取消</button>
          <button onClick={handleSave} disabled={saving}
            className="px-4 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50">
            {saving ? '保存中...' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}
