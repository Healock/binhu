import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Checkbox,
  Input,
  Modal,
  Select,
  Space,
  Tag,
  message,
} from 'antd'
import type { TableColumnsType } from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import AppTable from '../components/AppTable'
import { PageHeader, Panel } from '../components/ui'
import {
  createPermissionGroup,
  deletePermissionGroup,
  getPermissionCatalog,
  getPermissionGroups,
  updatePermissionGroup,
  updatePositionPermissionMappings,
  type PermissionCatalogItem,
  type PermissionGroupItem,
} from '../api/client'

const POSITIONS = ['中队长', '基础管控', '片长', '自购房', '组长', '组员']

export default function PermissionGroups() {
  const [groups, setGroups] = useState<PermissionGroupItem[]>([])
  const [catalog, setCatalog] = useState<PermissionCatalogItem[]>([])
  const [mappings, setMappings] = useState<Record<string, number>>({})
  const [editing, setEditing] = useState<PermissionGroupItem | null>(null)
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [dataScope, setDataScope] = useState<'all' | 'own_department'>('all')
  const [permissions, setPermissions] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [groupResult, catalogResult] = await Promise.all([
        getPermissionGroups(),
        getPermissionCatalog(),
      ])
      setGroups(groupResult.data)
      setMappings(groupResult.position_mappings)
      setCatalog(catalogResult.permissions)
    } catch (error: any) {
      message.error(error?.response?.data?.detail || '权限组加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const groupedCatalog = useMemo(() => {
    const result = new Map<string, PermissionCatalogItem[]>()
    catalog.forEach((item) => {
      result.set(item.category, [...(result.get(item.category) || []), item])
    })
    return [...result.entries()]
  }, [catalog])

  const openCreate = () => {
    setCreating(true)
    setEditing(null)
    setName('')
    setDescription('')
    setDataScope('all')
    setPermissions([])
  }

  const openEdit = (group: PermissionGroupItem) => {
    setCreating(false)
    setEditing(group)
    setName(group.name)
    setDescription(group.description || '')
    setDataScope(group.data_scope)
    setPermissions(group.permissions)
  }

  const saveGroup = async () => {
    if (!name.trim()) return
    setSaving(true)
    try {
      const payload = {
        name: name.trim(),
        description: description.trim(),
        permissions,
        data_scope: dataScope,
      }
      if (creating) {
        await createPermissionGroup(payload)
        message.success('自定义权限组已创建')
      } else if (editing) {
        const result = await updatePermissionGroup(editing.id, payload)
        message.success(`权限组已保存，影响 ${result.affected_users} 个账号`)
      }
      setEditing(null)
      setCreating(false)
      await load()
    } catch (error: any) {
      message.error(error?.response?.data?.detail || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const removeGroup = (group: PermissionGroupItem) => {
    Modal.confirm({
      title: `删除权限组“${group.name}”？`,
      content: '只有没有账号和岗位使用的自定义权限组才能删除。',
      okButtonProps: { danger: true },
      okText: '删除',
      cancelText: '取消',
      onOk: async () => {
        await deletePermissionGroup(group.id)
        message.success('权限组已删除')
        await load()
      },
    })
  }

  const saveMappings = async () => {
    if (POSITIONS.some(position => !mappings[position])) {
      message.error('请为每个岗位选择默认权限组')
      return
    }
    Modal.confirm({
      title: '保存岗位默认权限？',
      content: '所有“继承岗位”的账号会立即改用新的默认权限组。',
      okText: '确认保存',
      cancelText: '取消',
      onOk: async () => {
        const result = await updatePositionPermissionMappings(mappings)
        message.success(`已更新 ${result.affected_users} 个继承账号`)
        await load()
      },
    })
  }

  const columns: TableColumnsType<PermissionGroupItem> = [
    {
      title: '权限组', dataIndex: 'name', width: 180,
      render: (value, group) => (
        <div>
          <div className="font-medium text-slate-900">{value}</div>
          <div className="text-xs text-slate-500">{group.description}</div>
        </div>
      ),
    },
    {
      title: '数据范围', dataIndex: 'data_scope', width: 120,
      render: value => value === 'all' ? '全部社区' : '所属社区',
    },
    {
      title: '默认岗位', dataIndex: 'positions', width: 200,
      render: values => values?.length
        ? values.map((value: string) => <Tag key={value}>{value}</Tag>)
        : '-',
    },
    { title: '功能权限', dataIndex: 'permissions', width: 110, render: value => `${value.length} 项` },
    { title: '账号数', dataIndex: 'user_count', width: 90 },
    {
      title: '操作', width: 150,
      render: (_, group) => (
        <Space>
          <Button type="link" disabled={group.is_locked} onClick={() => openEdit(group)}>编辑</Button>
          {!group.is_system && (
            <Button type="link" danger onClick={() => removeGroup(group)}>删除</Button>
          )}
        </Space>
      ),
    },
  ]

  return (
    <div className="app-page space-y-5">
      <PageHeader
        title="权限组管理"
        description="岗位决定默认权限；单个账号也可以在用户管理中单独指定"
        actions={<Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建权限组</Button>}
      />
      <Panel title="岗位默认权限" description="人员岗位变化后，继承岗位的账号会自动跟随">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {POSITIONS.map(position => (
            <div key={position}>
              <label className="mb-1.5 block text-sm font-medium text-slate-700">{position}</label>
              <Select
                value={mappings[position]}
                onChange={value => setMappings(current => ({ ...current, [position]: value }))}
                options={groups.filter(group => !group.is_locked).map(group => ({ value: group.id, label: group.name }))}
                className="w-full"
              />
            </div>
          ))}
        </div>
        <div className="mt-4 flex justify-end">
          <Button type="primary" onClick={saveMappings}>保存岗位默认权限</Button>
        </div>
      </Panel>
      <Panel title="权限组" description="预设组不能删除；超级管理员组不能修改">
        <AppTable columns={columns} dataSource={groups} rowKey="id" loading={loading} scroll={{ x: 850 }} />
      </Panel>

      <Modal
        open={creating || Boolean(editing)}
        title={creating ? '新建自定义权限组' : `编辑 ${editing?.name || ''}`}
        okText="保存"
        cancelText="取消"
        confirmLoading={saving}
        onOk={saveGroup}
        onCancel={() => { setCreating(false); setEditing(null) }}
        width={720}
      >
        {editing && editing.user_count > 0 && (
          <Alert
            className="mb-4"
            type="warning"
            showIcon
            message={`保存后会影响 ${editing.user_count} 个账号，下一次请求立即生效。`}
          />
        )}
        <div className="space-y-4">
          <Input value={name} disabled={Boolean(editing?.is_system)} onChange={event => setName(event.target.value)} placeholder="权限组名称" />
          <Input.TextArea value={description} onChange={event => setDescription(event.target.value)} placeholder="简要说明" rows={2} />
          <Select
            value={dataScope}
            onChange={setDataScope}
            className="w-full"
            options={[
              { value: 'own_department', label: '只看所属社区' },
              { value: 'all', label: '查看全部社区' },
            ]}
          />
          {groupedCatalog.map(([category, items]) => (
            <div key={category}>
              <div className="mb-2 text-sm font-medium text-slate-800">{category}</div>
              <Checkbox.Group
                value={permissions}
                onChange={values => setPermissions(values.map(String))}
                className="grid gap-2 sm:grid-cols-2"
              >
                {items.map(item => (
                  <Checkbox key={item.code} value={item.code}>{item.label}</Checkbox>
                ))}
              </Checkbox.Group>
            </div>
          ))}
        </div>
      </Modal>
    </div>
  )
}
