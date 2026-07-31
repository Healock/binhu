import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Button, Input, Modal, Select, Switch, Tag, message } from 'antd'
import type { TableColumnsType } from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import AppTable from '../components/AppTable'
import { EmptyState, LoadingState, PageHeader } from '../components/ui'
import { getPermissionGroups, type PermissionGroupItem } from '../api/client'

interface UserItem {
  id: number
  username: string
  role: string
  member_id: number | null
  assignment_mode: 'inherited' | 'custom'
  password_is_temporary: boolean
  created_at?: string
  member: {
    id: number
    name: string
    position: string
    department_name: string | null
  } | null
  permission_group: { id: number; code: string; name: string } | null
}

interface MemberOption {
  id: number
  name: string
  position: string
  department?: { name: string } | null
}

function detailMessage(value: any, fallback: string) {
  const detail = value?.detail
  return typeof detail === 'object' ? detail.message || fallback : detail || fallback
}

export default function UserManagement() {
  const [users, setUsers] = useState<UserItem[]>([])
  const [members, setMembers] = useState<MemberOption[]>([])
  const [groups, setGroups] = useState<PermissionGroupItem[]>([])
  const [loading, setLoading] = useState(true)
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<UserItem | null>(null)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [memberId, setMemberId] = useState<number | null>(null)
  const [mode, setMode] = useState<'inherited' | 'custom'>('inherited')
  const [groupId, setGroupId] = useState<number | null>(null)
  const [temporary, setTemporary] = useState(true)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [userResponse, memberResponse, groupResponse] = await Promise.all([
        fetch('/api/users', { credentials: 'include' }),
        fetch('/api/grid-members?page_size=200', { credentials: 'include' }),
        getPermissionGroups(),
      ])
      if (!userResponse.ok || !memberResponse.ok) throw new Error('加载失败')
      setUsers((await userResponse.json()).data || [])
      setMembers((await memberResponse.json()).data || [])
      setGroups(groupResponse.data)
    } catch {
      message.error('用户和人员资料加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const availableMembers = useMemo(() => {
    const occupied = new Set(users.filter(user => user.id !== editing?.id).map(user => user.member_id))
    return members.filter(member => !occupied.has(member.id))
  }, [editing?.id, members, users])

  const beginCreate = () => {
    setEditing(null)
    setUsername('')
    setPassword('')
    setMemberId(null)
    setMode('inherited')
    setGroupId(null)
    setTemporary(true)
    setOpen(true)
  }

  const beginEdit = (user: UserItem) => {
    setEditing(user)
    setUsername(user.username)
    setPassword('')
    setMemberId(user.member_id)
    setMode(user.assignment_mode)
    setGroupId(user.permission_group?.id || null)
    setTemporary(user.password_is_temporary)
    setOpen(true)
  }

  const save = async () => {
    if (!editing && (!username.trim() || password.length < 8)) {
      message.error('请输入用户名和至少 8 个字符的初始密码')
      return
    }
    if (mode === 'inherited' && !memberId) {
      message.error('继承岗位权限时必须关联人员')
      return
    }
    if (mode === 'custom' && !groupId) {
      message.error('请选择权限组')
      return
    }
    setSaving(true)
    try {
      const payload: Record<string, unknown> = {
        member_id: memberId,
        assignment_mode: mode,
        permission_group_id: mode === 'custom' ? groupId : null,
        password_is_temporary: temporary,
      }
      if (password) payload.password = password
      if (!editing) payload.username = username.trim()
      const response = await fetch(
        editing ? `/api/users/${editing.id}` : '/api/users',
        {
          method: editing ? 'PUT' : 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-User-Activity': '1',
          },
          credentials: 'include',
          body: JSON.stringify(payload),
        },
      )
      if (!response.ok) throw await response.json()
      message.success(editing ? '账号已更新' : '账号已创建')
      setOpen(false)
      await load()
    } catch (error: any) {
      message.error(detailMessage(error, '保存失败'))
    } finally {
      setSaving(false)
    }
  }

  const remove = (user: UserItem) => Modal.confirm({
    title: `删除账号“${user.username}”？`,
    content: '账号的登录会话也会失效，人员资料不会被删除。',
    okText: '删除',
    cancelText: '取消',
    okButtonProps: { danger: true },
    onOk: async () => {
      const response = await fetch(`/api/users/${user.id}`, {
        method: 'DELETE',
        credentials: 'include',
        headers: { 'X-User-Activity': '1' },
      })
      if (!response.ok) throw new Error(detailMessage(await response.json(), '删除失败'))
      message.success('账号已删除')
      await load()
    },
  })

  const columns: TableColumnsType<UserItem> = [
    {
      title: '账号', dataIndex: 'username', width: 160,
      render: (value, user) => (
        <div>
          <div className="font-medium text-slate-900">{value}</div>
          {user.password_is_temporary && <Tag color="orange">临时密码</Tag>}
        </div>
      ),
    },
    {
      title: '关联人员', width: 220,
      render: (_, user) => user.member
        ? `${user.member.department_name || '未分配部门'} · ${user.member.name}（${user.member.position}）`
        : <span className="text-amber-600">未关联</span>,
    },
    {
      title: '权限组', width: 180,
      render: (_, user) => (
        <div>
          <div>{user.permission_group?.name || '待分配'}</div>
          <div className="text-xs text-slate-500">{user.assignment_mode === 'inherited' ? '继承岗位' : '单独指定'}</div>
        </div>
      ),
    },
    { title: '创建时间', dataIndex: 'created_at', width: 180, render: value => value || '-' },
    {
      title: '操作', width: 130,
      render: (_, user) => (
        <>
          <Button type="link" size="small" onClick={() => beginEdit(user)}>编辑</Button>
          <Button type="link" danger size="small" onClick={() => remove(user)}>删除</Button>
        </>
      ),
    },
  ]

  return (
    <div className="app-page">
      <PageHeader
        title="用户管理"
        description="关联人员后可继承岗位权限，也可以为单个账号指定权限组"
        actions={<Button type="primary" icon={<PlusOutlined />} onClick={beginCreate}>添加账号</Button>}
      />
      {loading ? <LoadingState /> : users.length === 0 ? <EmptyState label="暂无用户" /> : (
        <AppTable columns={columns} dataSource={users} rowKey="id" scroll={{ x: 900 }} />
      )}

      <Modal
        open={open}
        title={editing ? '编辑账号' : '添加账号'}
        okText="保存"
        cancelText="取消"
        confirmLoading={saving}
        onOk={save}
        onCancel={() => setOpen(false)}
      >
        <div className="space-y-4 pt-2">
          <div>
            <label className="mb-1.5 block text-sm font-medium">用户名</label>
            <Input value={username} disabled={Boolean(editing)} onChange={event => setUsername(event.target.value)} />
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium">{editing ? '新密码（留空不改）' : '统一初始密码'}</label>
            <Input.Password value={password} onChange={event => setPassword(event.target.value)} />
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium">关联人员</label>
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              value={memberId}
              onChange={setMemberId}
              className="w-full"
              options={availableMembers.map(member => ({
                value: member.id,
                label: `${member.department?.name || '未分配部门'} · ${member.name}（${member.position}）`,
              }))}
            />
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium">权限来源</label>
            <Select
              value={mode}
              onChange={setMode}
              className="w-full"
              options={[
                { value: 'inherited', label: '继承人员岗位（推荐）' },
                { value: 'custom', label: '单独指定权限组' },
              ]}
            />
          </div>
          {mode === 'custom' && (
            <div>
              <label className="mb-1.5 block text-sm font-medium">权限组</label>
              <Select
                value={groupId}
                onChange={setGroupId}
                className="w-full"
                options={groups.map(group => ({ value: group.id, label: group.name }))}
              />
            </div>
          )}
          <div className="flex items-center justify-between rounded-lg border border-slate-200 p-3">
            <div>
              <div className="text-sm font-medium">临时密码提醒</div>
              <div className="text-xs text-slate-500">不强制改密，但登录后持续提醒</div>
            </div>
            <Switch checked={temporary} onChange={setTemporary} />
          </div>
          {!memberId && <Alert type="warning" showIcon message="未关联人员的低权限账号不能查看业务数据。" />}
        </div>
      </Modal>
    </div>
  )
}
