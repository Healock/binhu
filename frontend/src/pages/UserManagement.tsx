import { useState, useEffect, useCallback } from 'react'
import { ROLE_LABELS } from '../types'
import type { Role } from '../types'
import { getDisplayMode } from '../utils/displayMode'

interface UserItem {
  id: number
  username: string
  role: Role
  role_label: string
  created_at?: string
}

export default function UserManagement() {
  const [users, setUsers] = useState<UserItem[]>([])
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [editingUser, setEditingUser] = useState<UserItem | null>(null)
  const [formUsername, setFormUsername] = useState('')
  const [formPassword, setFormPassword] = useState('')
  const [formRole, setFormRole] = useState<Role>('member')
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')

  const fetchUsers = useCallback(async () => {
    try {
      const res = await fetch('/api/users', { credentials: 'include' })
      const data = await res.json()
      setUsers(data.data || [])
    } catch {} finally { setLoading(false) }
  }, [])

  useEffect(() => { fetchUsers() }, [fetchUsers])

  const handleSave = async () => {
    if (!formUsername.trim() || (!editingUser && !formPassword)) return
    setSaving(true); setMsg('')
    try {
      if (editingUser) {
        // 编辑用户
        const body: Record<string, string> = {}
        if (formRole !== editingUser.role) body.role = formRole
        if (formPassword) body.password = formPassword
        const res = await fetch(`/api/users/${editingUser.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify(body),
        })
        if (!res.ok) { const d = await res.json(); throw new Error(d.detail || '修改失败') }
        setMsg('修改成功')
      } else {
        // 创建用户
        const res = await fetch('/api/users', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify({ username: formUsername.trim(), password: formPassword, role: formRole }),
        })
        if (!res.ok) { const d = await res.json(); throw new Error(d.detail || '创建失败') }
        setMsg('创建成功')
      }
      setShowForm(false); setEditingUser(null)
      setFormUsername(''); setFormPassword(''); setFormRole('member')
      fetchUsers()
    } catch (e: any) { setMsg(e.message || '操作失败') }
    finally { setSaving(false) }
  }

  const handleEdit = (user: UserItem) => {
    setEditingUser(user)
    setFormUsername(user.username)
    setFormPassword('')
    setFormRole(user.role)
    setShowForm(true)
    setMsg('')
  }

  const handleDelete = async (id: number, username: string) => {
    if (!confirm(`确认删除用户"${username}"？`)) return
    try {
      const res = await fetch(`/api/users/${id}`, { method: 'DELETE', credentials: 'include' })
      if (!res.ok) { const d = await res.json(); throw new Error(d.detail || '删除失败') }
      setMsg('删除成功')
      fetchUsers()
    } catch (e: any) { setMsg(e.message) }
  }

  const handleAdd = () => {
    setEditingUser(null)
    setFormUsername(''); setFormPassword(''); setFormRole('member')
    setShowForm(true)
    setMsg('')
  }

  const roleOptions: Role[] = ['super_admin', 'admin', 'leader', 'member']
  const cardMode = getDisplayMode() === 'card'

  return (
    <div className="space-y-4">
      <div className="bg-white rounded-lg shadow p-4">
        <div className="flex items-center gap-3">
          <h2 className="text-base font-semibold text-gray-800">用户管理</h2>
          <button onClick={handleAdd} className="px-4 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700">添加用户</button>
          <span className="text-sm text-gray-500 ml-auto">共 {users.length} 个用户</span>
        </div>
        {msg && <p className={`text-sm mt-2 ${msg.includes('成功') ? 'text-green-600' : 'text-red-500'}`}>{msg}</p>}
      </div>

      {showForm && (
        <div className="bg-white rounded-lg shadow p-4">
          <h3 className="text-sm font-semibold text-gray-700 mb-3">{editingUser ? '编辑用户' : '添加用户'}</h3>
          <div className="flex flex-wrap gap-3 items-end">
            <div>
              <label className="block text-xs text-gray-500 mb-1">用户名</label>
              <input value={formUsername} onChange={(e) => setFormUsername(e.target.value)}
                disabled={!!editingUser}
                className="border border-gray-300 rounded px-3 py-1.5 text-sm w-40 disabled:bg-gray-100" />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">{editingUser ? '新密码（留空不改）' : '密码'}</label>
              <input type="password" value={formPassword} onChange={(e) => setFormPassword(e.target.value)}
                className="border border-gray-300 rounded px-3 py-1.5 text-sm w-40" />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">角色</label>
              <select value={formRole} onChange={(e) => setFormRole(e.target.value as Role)}
                className="border border-gray-300 rounded px-3 py-1.5 text-sm">
                {roleOptions.map(r => <option key={r} value={r}>{ROLE_LABELS[r]}</option>)}
              </select>
            </div>
            <button onClick={handleSave} disabled={saving}
              className="px-4 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50">
              {saving ? '保存中...' : '保存'}
            </button>
            <button onClick={() => setShowForm(false)}
              className="px-4 py-1.5 border border-gray-300 rounded text-sm hover:bg-gray-50">取消</button>
          </div>
        </div>
      )}

      <div className="bg-white rounded-lg shadow overflow-auto">
        {loading ? (
          <p className="p-8 text-center text-gray-400 text-sm">加载中...</p>
        ) : users.length === 0 ? (
          <p className="p-8 text-center text-gray-400 text-sm">暂无用户</p>
        ) : cardMode ? (
          <div className="grid grid-cols-1 gap-3 p-4">
            {users.map((u) => (
              <div key={u.id} className="border border-gray-200 rounded-lg p-4 flex items-center justify-between">
                <div>
                  <div className="font-medium text-gray-800">{u.username}</div>
                  <div className="text-sm text-gray-500">{ROLE_LABELS[u.role] || u.role}</div>
                  {u.created_at && <div className="text-xs text-gray-400">创建于 {u.created_at}</div>}
                </div>
                <div className="flex gap-2">
                  <button onClick={() => handleEdit(u)} className="text-xs text-blue-500 hover:underline">编辑</button>
                  <button onClick={() => handleDelete(u.id, u.username)} className="text-xs text-red-500 hover:underline">删除</button>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <table className="min-w-full text-sm">
            <thead className="bg-gray-50 border-b sticky top-0 z-10">
              <tr>
                <th className="px-3 py-2 text-left font-medium text-gray-600">用户名</th>
                <th className="px-3 py-2 text-left font-medium text-gray-600">角色</th>
                <th className="px-3 py-2 text-left font-medium text-gray-600">创建时间</th>
                <th className="px-3 py-2 text-left font-medium text-gray-600">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {users.map((u) => (
                <tr key={u.id} className="hover:bg-gray-50">
                  <td className="px-3 py-2 text-gray-800 font-medium">{u.username}</td>
                  <td className="px-3 py-2 text-gray-700">{ROLE_LABELS[u.role] || u.role}</td>
                  <td className="px-3 py-2 text-gray-500 text-xs">{u.created_at || '-'}</td>
                  <td className="px-3 py-2">
                    <button onClick={() => handleEdit(u)} className="text-xs text-blue-500 hover:underline mr-3">编辑</button>
                    <button onClick={() => handleDelete(u.id, u.username)} className="text-xs text-red-500 hover:underline">删除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
