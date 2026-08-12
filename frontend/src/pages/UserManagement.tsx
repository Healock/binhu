import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Button, Input, Modal, Select, Switch, Tag, message } from 'antd'
import type { TableColumnsType } from 'antd'
import { PlusOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons'
import { Link } from 'react-router-dom'
import AppTable from '../components/AppTable'
import { EmptyState, ListToolbar, LoadingState, PageHeader } from '../components/ui'
import {
  fetchWithAuth,
  getPermissionGroups,
  type PermissionGroupItem,
} from '../api/client'
import useSystemTime from '../hooks/useSystemTime'

interface UserItem {
  id: number
  username: string
  display_name: string
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
  permission_groups: Array<{ id: number; code: string; name: string }>
}

interface MemberOption {
  id: number
  name: string
  position: string
  department?: { name: string } | null
  departments?: Array<{ name: string }>
}

function detailMessage(value: any, fallback: string) {
  const detail = value?.detail
  return typeof detail === 'object' ? detail.message || fallback : detail || fallback
}

export default function UserManagement() {
  const formatTime = useSystemTime()
  const [users, setUsers] = useState<UserItem[]>([])
  const [members, setMembers] = useState<MemberOption[]>([])
  const [groups, setGroups] = useState<PermissionGroupItem[]>([])
  const [loading, setLoading] = useState(true)
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<UserItem | null>(null)
  const [username, setUsername] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [memberId, setMemberId] = useState<number | null>(null)
  const [mode, setMode] = useState<'inherited' | 'custom'>('inherited')
  const [groupIds, setGroupIds] = useState<number[]>([])
  const [temporary, setTemporary] = useState(true)
  const [saving, setSaving] = useState(false)
  const [keyword, setKeyword] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [userResponse, memberResponse, groupResponse] = await Promise.all([
        fetchWithAuth('/api/users'),
        fetchWithAuth('/api/grid-members?page_size=200'),
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
    setDisplayName('')
    setPassword('')
    setMemberId(null)
    setMode('inherited')
    setGroupIds([])
    setTemporary(true)
    setOpen(true)
  }

  const beginEdit = (user: UserItem) => {
    setEditing(user)
    setUsername(user.username)
    setDisplayName(user.display_name)
    setPassword('')
    setMemberId(user.member_id)
    setMode(user.assignment_mode)
    setGroupIds(
      user.permission_groups?.map(group => group.id)
      || (user.permission_group ? [user.permission_group.id] : []),
    )
    setTemporary(user.password_is_temporary)
    setOpen(true)
  }

  const save = async () => {
    if (!displayName.trim()) {
      message.error('请输入姓名')
      return
    }
    if (!editing && (!username.trim() || password.length < 8)) {
      message.error('请输入用户名和至少 8 个字符的初始密码')
      return
    }
    if (mode === 'inherited' && !memberId) {
      message.error('继承岗位权限时必须关联人员')
      return
    }
    if (mode === 'custom' && !groupIds.length) {
      message.error('请选择权限组')
      return
    }
    setSaving(true)
    try {
      const payload: Record<string, unknown> = {
        display_name: displayName.trim(),
        member_id: memberId,
        assignment_mode: mode,
        permission_group_ids: mode === 'custom' ? groupIds : null,
        password_is_temporary: temporary,
      }
      if (password) payload.password = password
      if (!editing) payload.username = username.trim()
      const response = await fetchWithAuth(
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
      const response = await fetchWithAuth(`/api/users/${user.id}`, {
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
      title: '姓名', dataIndex: 'display_name', width: 140,
      render: (value, user) => (
        <div>
          <Link
            className="font-medium text-[var(--app-primary)] hover:underline"
            to={`/people/${user.id}`}
            state={{ returnTo: '/users', returnLabel: '返回用户管理' }}
          >
            {user.member?.name || value}
          </Link>
          {user.password_is_temporary && <Tag color="orange">临时密码</Tag>}
        </div>
      ),
    },
    { title: '登录用户名', dataIndex: 'username', width: 160 },
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
          <div className="flex flex-wrap gap-1">
            {(user.permission_groups?.length
              ? user.permission_groups
              : user.permission_group ? [user.permission_group] : []
            ).map(group => <Tag key={group.id} className="m-0">{group.name}</Tag>)}
            {!user.permission_groups?.length && !user.permission_group && '待分配'}
          </div>
          <div className="text-xs text-slate-500">{user.assignment_mode === 'inherited' ? '继承岗位' : '单独指定'}</div>
        </div>
      ),
    },
    { title: '创建时间', dataIndex: 'created_at', width: 180, render: formatTime },
    {
      title: '操作', width: 130,
      render: (_, user) => (
        <>
          <Button type="link" size="small" onClick={() => beginEdit(user)}>编辑</Button>
          <Button
            type="link"
            danger
            size="small"
            disabled={user.member_id !== null}
            title={user.member_id !== null ? '请到人员管理联动删除' : undefined}
            onClick={() => remove(user)}
          >
            删除
          </Button>
        </>
      ),
    },
  ]
  const normalizedKeyword = keyword.trim().toLocaleLowerCase()
  const visibleUsers = normalizedKeyword
    ? users.filter(user => [
      user.display_name,
      user.username,
      user.member?.name,
      user.member?.department_name,
      user.member?.position,
      ...(user.permission_groups || []).map(group => group.name),
    ].filter(Boolean).join(' ').toLocaleLowerCase().includes(normalizedKeyword))
    : users

  return (
    <div className="app-page">
      <PageHeader
        title="用户管理"
        description="人员账号在人员管理中建立并保持一对一；这里仍可维护系统账号和自定义权限"
      />
      <ListToolbar
        filters={<Input allowClear prefix={<SearchOutlined />} value={keyword} onChange={event => setKeyword(event.target.value)} placeholder="搜索姓名、账号、部门或权限组" className="w-full md:w-80" />}
        meta={<span>当前 {visibleUsers.length} 个账号</span>}
        actions={<><Button icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button><Button type="primary" icon={<PlusOutlined />} onClick={beginCreate}>添加账号</Button></>}
      />
      {loading ? <LoadingState /> : visibleUsers.length === 0 ? <EmptyState label={users.length ? '没有符合条件的账号' : '暂无用户'} /> : (
        <AppTable columns={columns} dataSource={visibleUsers} rowKey="id" scroll={{ x: 900 }} />
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
            <label className="mb-1.5 block text-sm font-medium">姓名</label>
            <Input
              value={displayName}
              maxLength={100}
              placeholder="用于平台内显示"
              onChange={event => setDisplayName(event.target.value)}
            />
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium">登录用户名</label>
            <Input value={username} disabled={Boolean(editing)} onChange={event => setUsername(event.target.value)} />
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium">{editing ? '新密码（留空不改）' : '统一初始密码'}</label>
            <Input.Password value={password} onChange={event => setPassword(event.target.value)} />
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium">关联人员</label>
            <Select
              allowClear={!editing?.member_id}
              showSearch
              optionFilterProp="label"
              value={memberId}
              disabled={Boolean(editing?.member_id)}
              onChange={(value) => {
                setMemberId(value)
                if (!displayName.trim() && value) {
                  setDisplayName(availableMembers.find(member => member.id === value)?.name || '')
                }
              }}
              className="w-full"
              options={availableMembers.map(member => ({
                value: member.id,
                label: `${member.departments?.map(item => item.name).join('、') || member.department?.name || '未分配部门'} · ${member.name}（${member.position}）`,
              }))}
            />
            {editing?.member_id && (
              <p className="mt-1.5 text-xs text-slate-500">
                已关联账号不能在此解除或改绑；误建资料由超级管理员在人员管理中联动删除。
              </p>
            )}
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
                mode="multiple"
                value={groupIds}
                onChange={setGroupIds}
                className="w-full"
                options={groups.map(group => ({
                  value: group.id,
                  label: group.name,
                  disabled: group.code === 'super_admin'
                    ? groupIds.some(id => id !== group.id)
                    : groups.some(candidate => (
                        candidate.code === 'super_admin'
                        && groupIds.includes(candidate.id)
                      )),
                }))}
                maxTagCount="responsive"
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
