import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Input, Modal, Select, Space, Switch, Tag, message } from 'antd'
import type { TableColumnsType } from 'antd'
import AppTable from '../components/AppTable'
import { PageHeader, Panel } from '../components/ui'
import {
  listFunctionModules,
  updateFunctionModule,
  type FunctionModuleItem,
} from '../api/client'

const OWNER_OPTIONS = [
  { value: 'common', label: '公共功能' },
  { value: 'community_police', label: '社区警务' },
  { value: 'internal', label: '平台内部' },
]

export default function FunctionModulesSettings() {
  const [items, setItems] = useState<FunctionModuleItem[]>([])
  const [loading, setLoading] = useState(true)
  const [editing, setEditing] = useState<FunctionModuleItem | null>(null)
  const [saving, setSaving] = useState(false)
  const [name, setName] = useState('')
  const [ownerUnit, setOwnerUnit] = useState('')
  const [isPublic, setIsPublic] = useState(false)
  const [requiredPermissions, setRequiredPermissions] = useState('')
  const [route, setRoute] = useState('')
  const [mobileVisible, setMobileVisible] = useState(true)
  const [enabled, setEnabled] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setItems((await listFunctionModules()).data)
    } catch (error: any) {
      message.error(error?.response?.data?.detail || '功能模块加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const openEdit = (item: FunctionModuleItem) => {
    setEditing(item)
    setName(item.name)
    setOwnerUnit(item.owner_unit)
    setIsPublic(item.is_public)
    setRequiredPermissions(item.required_permissions.join(', '))
    setRoute(item.route)
    setMobileVisible(item.mobile_visible)
    setEnabled(item.enabled)
  }

  const save = async () => {
    if (!editing || !name.trim()) return
    setSaving(true)
    try {
      await updateFunctionModule(editing.module_id, {
        name: name.trim(),
        owner_unit: ownerUnit,
        is_public: isPublic,
        required_permissions: requiredPermissions.split(',').map(value => value.trim()).filter(Boolean),
        route: route.trim(),
        mobile_visible: mobileVisible,
        enabled,
      })
      message.success('功能模块已保存，新的访问快照将在下一次请求生效')
      setEditing(null)
      await load()
    } catch (error: any) {
      message.error(error?.response?.data?.detail || '功能模块保存失败')
    } finally {
      setSaving(false)
    }
  }

  const columns: TableColumnsType<FunctionModuleItem> = [
    { title: '模块', dataIndex: 'name', width: 180, render: (value, item) => <div><div className="font-medium">{value}</div><div className="text-xs text-slate-500">{item.module_id}</div></div> },
    { title: '归属单位', dataIndex: 'owner_unit', width: 130, render: value => OWNER_OPTIONS.find(item => item.value === value)?.label || value },
    { title: '访问路由', dataIndex: 'route', width: 320, render: value => <span className="break-all text-xs">{value || '未配置'}</span> },
    { title: '状态', dataIndex: 'enabled', width: 100, render: value => value ? <Tag color="green">启用</Tag> : <Tag>停用</Tag> },
    { title: '手机端', dataIndex: 'mobile_visible', width: 100, render: value => value ? '可见' : '隐藏' },
    { title: '版本', dataIndex: 'version', width: 70 },
    { title: '操作', width: 90, fixed: 'right', render: (_, item) => <Button type="link" onClick={() => openEdit(item)}>编辑</Button> },
  ]

  return (
    <div className="app-page min-w-0">
      <PageHeader title="功能模块" description="维护单位归属和访问门禁；保存后下一次请求读取新版本快照。" />
      <Panel>
        <div className="grid gap-3">
          <Alert type="info" showIcon message="模块门禁与原有功能权限同时生效。上线前请先确认在用账号已关联正确部门，停用模块会同时阻止直接访问接口。" />
          <AppTable rowKey="module_id" loading={loading} columns={columns} dataSource={items} scroll={{ x: 980 }} pagination={false} />
        </div>
      </Panel>
      <Modal title={editing ? `编辑模块：${editing.name}` : '编辑模块'} open={Boolean(editing)} onCancel={() => setEditing(null)} onOk={save} confirmLoading={saving} okText="保存" cancelText="取消" width={620}>
        <div className="grid gap-4 py-2">
          <Alert type="warning" showIcon message="路由前缀使用站内路径，多个前缀用英文逗号分隔。不要把模块路由改到未验收的业务页面。" />
          <label className="grid gap-1"><span>模块名称</span><Input value={name} onChange={event => setName(event.target.value)} /></label>
          <label className="grid gap-1"><span>归属单位</span><Select value={ownerUnit} options={OWNER_OPTIONS} onChange={setOwnerUnit} /></label>
          <label className="grid gap-1"><span>访问路由前缀</span><Input value={route} onChange={event => setRoute(event.target.value)} /></label>
          <label className="grid gap-1"><span>必需权限（英文逗号分隔）</span><Input value={requiredPermissions} onChange={event => setRequiredPermissions(event.target.value)} /></label>
          <Space wrap>
            <label className="flex items-center gap-2"><Switch checked={isPublic} onChange={setIsPublic} />公共模块</label>
            <label className="flex items-center gap-2"><Switch checked={mobileVisible} onChange={setMobileVisible} />手机端可见</label>
            <label className="flex items-center gap-2"><Switch checked={enabled} onChange={setEnabled} />启用</label>
          </Space>
        </div>
      </Modal>
    </div>
  )
}
