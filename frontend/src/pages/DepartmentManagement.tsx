import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Alert, Button, Input, Modal, Select, Space, Tag, message } from 'antd'
import { DeleteOutlined, EditOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { PageHeader, Panel } from '../components/ui'
import {
  createDepartment,
  deleteDepartment,
  getCommunityAreas,
  getDepartments,
  updateDepartment,
  updateDepartmentStatus,
  type DepartmentOption,
} from '../api/client'

type Draft = {
  name: string
  department_type: 'unit'
  parent_id: number | null
  area_id: number | null
}

const emptyDraft = (): Draft => ({ name: '', department_type: 'unit', parent_id: null, area_id: null })

export default function DepartmentManagement() {
  const [departments, setDepartments] = useState<DepartmentOption[]>([])
  const [areas, setAreas] = useState<Array<{ id: number; name: string }>>([])
  const [loading, setLoading] = useState(true)
  const [editing, setEditing] = useState<DepartmentOption | null>(null)
  const [creating, setCreating] = useState(false)
  const [draft, setDraft] = useState<Draft>(emptyDraft())
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [departmentRows, areaRows] = await Promise.all([getDepartments(), getCommunityAreas()])
      setDepartments(departmentRows)
      setAreas(areaRows.map(item => ({ id: item.id, name: item.name })))
      setError('')
    } catch (err: any) {
      setError(err?.response?.data?.detail || '部门列表加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const byParent = useMemo(() => {
    const result = new Map<number | null, DepartmentOption[]>()
    departments.forEach(item => result.set(item.parent_id ?? null, [...(result.get(item.parent_id ?? null) || []), item]))
    result.forEach(rows => rows.sort((a, b) => a.name.localeCompare(b.name, 'zh-CN')))
    return result
  }, [departments])

  const descendantsById = useMemo(() => {
    const result = new Map<number, Set<number>>()
    departments.forEach(root => {
      const found = new Set<number>()
      const visit = (id: number) => departments.filter(item => item.parent_id === id).forEach(item => {
        if (found.has(item.id)) return
        found.add(item.id)
        visit(item.id)
      })
      visit(root.id)
      result.set(root.id, found)
    })
    return result
  }, [departments])

  const openCreate = () => {
    setEditing(null); setDraft(emptyDraft()); setError(''); setCreating(true)
  }
  const openEdit = (item: DepartmentOption) => {
    setEditing(item)
    setDraft({ name: item.name, department_type: 'unit', parent_id: item.parent_id ?? null, area_id: item.area_id ?? null })
    setError('')
  }

  const save = async () => {
    if (!draft.name.trim()) { setError('部门名称不能为空'); return }
    setSaving(true); setError('')
    try {
      if (editing) await updateDepartment(editing.id, draft)
      else await createDepartment(draft)
      setCreating(false); setEditing(null); await load(); message.success(editing ? '部门已更新' : '部门已创建')
    } catch (err: any) {
      const detail = err?.response?.data?.detail
      setError(typeof detail === 'object' ? detail.message || '保存失败' : detail || '保存失败')
    } finally { setSaving(false) }
  }

  const toggle = async (item: DepartmentOption) => {
    try { await updateDepartmentStatus(item.id, !item.is_active); await load() }
    catch (err: any) {
      const detail = err?.response?.data?.detail
      const text = typeof detail === 'object'
        ? `${detail.message || '操作失败'}。${detail.next_step || ''}${detail.allowed_role ? ` 可处理角色：${detail.allowed_role}。` : ''}${detail.recovery || ''}`
        : detail || '操作失败'
      Modal.error({ title: '无法变更部门状态', content: text })
    }
  }

  const remove = (item: DepartmentOption) => Modal.confirm({
    title: `删除“${item.name}”？`, content: '删除只适用于没有成员、没有下级且不代表社区的单位部门。', okButtonProps: { danger: true },
    onOk: async () => { try { await deleteDepartment(item.id); await load(); message.success('部门已删除') } catch (err: any) { message.error(err?.response?.data?.detail?.message || err?.response?.data?.detail || '删除失败') } },
  })

  const renderRows = (parentId: number | null, depth = 0, visited = new Set<number>()): ReactNode[] => (byParent.get(parentId) || []).flatMap(item => {
    if (visited.has(item.id)) return []
    const nextVisited = new Set(visited).add(item.id)
    return [
      <div key={item.id} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-2 border-b border-slate-100 px-3 py-3" style={{ paddingLeft: `${12 + depth * 28}px` }}>
        <div className="min-w-0"><div className="truncate font-medium text-slate-800">{item.name}</div><div className="text-xs text-slate-500">{item.type === 'community' ? '社区级部门' : item.type === 'internal' ? '历史内勤部门' : '单位级部门'}{item.area_id ? ` · 片区 ${areas.find(area => area.id === item.area_id)?.name || ''}` : ''}</div></div>
        <div className="flex flex-wrap items-center justify-end gap-2"><Tag color={item.is_active ? 'green' : undefined}>{item.is_active ? '启用' : '停用'}</Tag><span className="text-xs text-slate-500">成员 {item.member_count || 0} · 下级 {item.child_count || 0}</span>{item.type !== 'internal' && <Space size={4}><Button size="small" icon={<EditOutlined />} onClick={() => openEdit(item)}>编辑</Button>{!item.community_name && <Button size="small" icon={<DeleteOutlined />} danger onClick={() => remove(item)}>删除</Button>}{!item.community_name && <Button size="small" onClick={() => toggle(item)}>{item.is_active ? '停用' : '启用'}</Button>}</Space>}</div>
      </div>,
      ...renderRows(item.id, depth + 1, nextVisited),
    ]
  })

  return <div className="app-page min-w-0">
    <PageHeader title="部门管理" description="维护行政编成与部门树；权限组仍负责能做什么，部门归属只负责功能归属和组织层级。" actions={<Space><Button icon={<ReloadOutlined />} onClick={load}>刷新</Button><Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新建单位部门</Button></Space>} />
    {error && !creating && !editing && <Alert type="error" showIcon message={error} className="mb-4" />}
    <Panel className="grid gap-4">
      <Alert type="info" showIcon message="社区部门由社区管理维护；本页用于建立社区警务队、片区、基础管控、管家码、综合指挥室等单位级部门，并设置上级关系。暂时无部门的人员可以保持未分配。" />
      <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 border-b border-slate-200 px-3 py-2 text-xs font-medium text-slate-500"><span>部门树</span><span>状态、数量与操作</span></div>
      {loading ? <div className="p-8 text-center text-slate-500">正在加载…</div> : renderRows(null)}
      {!loading && !departments.length && <div className="p-8 text-center text-slate-500">暂无部门</div>}
    </Panel>
    <Modal open={creating || !!editing} title={editing ? `编辑${editing.type === 'community' ? '社区' : '单位'}部门` : '新建单位部门'} okText="保存" cancelText="取消" confirmLoading={saving} onOk={save} onCancel={() => { setCreating(false); setEditing(null) }}>
      {error && <Alert className="mb-4" type="error" showIcon message={error} />}
      <div className="grid gap-4"><label className="grid gap-1 text-sm">名称<Input value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })} placeholder="例如：社区警务队" /></label><label className="grid gap-1 text-sm">上级部门<Select allowClear value={draft.parent_id ?? undefined} onChange={value => setDraft({ ...draft, parent_id: value ?? null })} options={departments.filter(item => item.type === 'unit' && item.id !== editing?.id && !descendantsById.get(editing?.id || 0)?.has(item.id)).map(item => ({ value: item.id, label: item.name }))} placeholder="顶层部门可留空" /></label><label className="grid gap-1 text-sm">关联片区（可选）<Select allowClear value={draft.area_id ?? undefined} onChange={value => setDraft({ ...draft, area_id: value ?? null })} options={areas.map(item => ({ value: item.id, label: item.name }))} placeholder="片区部门可关联现有片区" /></label></div>
    </Modal>
  </div>
}
