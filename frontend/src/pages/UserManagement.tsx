import { useState, useEffect, useCallback } from 'react'
import { Alert, Button, Input, Modal, Select, Tag } from 'antd'
import type { TableColumnsType } from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import AppTable from '../components/AppTable'
import { ROLE_LABELS } from '../types'
import type { Role } from '../types'
import { EmptyState, LoadingState, PageHeader } from '../components/ui'

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
  const [loadError, setLoadError] = useState('')

  const fetchUsers = useCallback(async () => {
    setLoading(true)
    setLoadError('')
    try {
      const res = await fetch('/api/users', { credentials: 'include' })
      if (!res.ok) throw new Error('用户列表加载失败')
      const data = await res.json()
      setUsers(data.data || [])
    } catch {
      setLoadError('用户列表加载失败，请稍后重试')
    } finally { setLoading(false) }
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

  const handleDelete = (id: number, username: string) => {
    Modal.confirm({
      title: '删除用户',
      content: `确认删除用户“${username}”？`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          const res = await fetch(`/api/users/${id}`, { method: 'DELETE', credentials: 'include' })
          if (!res.ok) { const d = await res.json(); throw new Error(d.detail || '删除失败') }
          setMsg('删除成功')
          fetchUsers()
        } catch (e: any) {
          setMsg(e.message || '删除失败')
        }
      },
    })
  }

  const handleAdd = () => {
    setEditingUser(null)
    setFormUsername(''); setFormPassword(''); setFormRole('member')
    setShowForm(true)
    setMsg('')
  }

  const roleOptions: Role[] = ['super_admin', 'admin', 'leader', 'member']
  const userColumns: TableColumnsType<UserItem> = [
    {
      title: '用户名',
      dataIndex: 'username',
      key: 'username',
      width: 200,
      sorter: (left, right) => left.username.localeCompare(right.username, 'zh-CN'),
      render: value => <span className="font-medium text-slate-800">{value}</span>,
    },
    {
      title: '角色',
      dataIndex: 'role',
      key: 'role',
      width: 160,
      sorter: (left, right) => (
        (ROLE_LABELS[left.role] || left.role).localeCompare(
          ROLE_LABELS[right.role] || right.role,
          'zh-CN',
        )
      ),
      render: value => ROLE_LABELS[value] || value,
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 200,
      sorter: (left, right) => (left.created_at || '').localeCompare(right.created_at || ''),
      render: value => <span className="text-xs text-slate-500">{value || '-'}</span>,
    },
    {
      title: '操作',
      key: 'actions',
      width: 130,
      render: (_, user) => (
        <>
          <Button type="link" size="small" onClick={() => handleEdit(user)}>编辑</Button>
          <Button type="link" danger size="small" onClick={() => handleDelete(user.id, user.username)}>删除</Button>
        </>
      ),
    },
  ]

  return (
    <div className="app-page">
      <PageHeader
        title="用户管理"
        description="管理登录账号和系统角色，仅超级管理员可以使用"
        actions={
          <>
            <Tag color="blue">共 {users.length} 个用户</Tag>
            <Button type="primary" icon={<PlusOutlined />} onClick={handleAdd}>添加用户</Button>
          </>
        }
      />

      {msg && !showForm && (
        <Alert type={msg.includes('成功') ? 'success' : 'error'} showIcon message={msg} />
      )}

      {showForm && (
        <Modal
          open
          title={editingUser ? '编辑用户' : '添加用户'}
          okText="保存"
          cancelText="取消"
          confirmLoading={saving}
          onOk={handleSave}
          onCancel={() => { setShowForm(false); setEditingUser(null); setMsg('') }}
        >
          <div className="space-y-4 pt-2">
            <div>
              <label className="mb-1.5 block text-sm font-medium text-slate-700">用户名</label>
              <Input
                value={formUsername}
                onChange={event => setFormUsername(event.target.value)}
                disabled={!!editingUser}
                placeholder="请输入用户名"
              />
            </div>
            <div>
              <label className="mb-1.5 block text-sm font-medium text-slate-700">{editingUser ? '新密码（留空不改）' : '密码'}</label>
              <Input.Password
                value={formPassword}
                onChange={event => setFormPassword(event.target.value)}
                placeholder={editingUser ? '不修改密码可留空' : '请输入密码'}
              />
            </div>
            <div>
              <label className="mb-1.5 block text-sm font-medium text-slate-700">角色</label>
              <Select
                value={formRole}
                onChange={value => setFormRole(value as Role)}
                className="w-full"
                options={roleOptions.map(role => ({ value: role, label: ROLE_LABELS[role] }))}
              />
            </div>
            {msg && <p className="text-sm text-red-700">{msg}</p>}
          </div>
        </Modal>
      )}

      {loading ? (
        <div className="app-table-wrap">
          <LoadingState />
        </div>
      ) : loadError ? (
        <div className="app-table-wrap">
          <EmptyState label={loadError} />
        </div>
      ) : users.length === 0 ? (
        <div className="app-table-wrap">
          <EmptyState label="暂无用户" />
        </div>
      ) : (
        <>
          <div className="app-table-wrap md:hidden">
            <div className="grid grid-cols-1 gap-3 p-4">
              {users.map((u) => (
                <div key={u.id} className="border border-gray-200 rounded-lg p-4 flex items-center justify-between">
                  <div>
                    <div className="font-medium text-gray-800">{u.username}</div>
                    <div className="text-sm text-gray-500">{ROLE_LABELS[u.role] || u.role}</div>
                    {u.created_at && <div className="text-xs text-gray-400">创建于 {u.created_at}</div>}
                  </div>
                  <div className="flex gap-2">
                    <Button type="link" size="small" onClick={() => handleEdit(u)}>编辑</Button>
                    <Button type="link" danger size="small" onClick={() => handleDelete(u.id, u.username)}>删除</Button>
                  </div>
                </div>
              ))}
            </div>
          </div>
          <div className="hidden md:block">
            <AppTable<UserItem>
              columns={userColumns}
              dataSource={users}
              rowKey="id"
              scroll={{ x: 690 }}
            />
          </div>
        </>
      )}
    </div>
  )
}
