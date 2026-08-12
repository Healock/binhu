import { useEffect, useMemo, useState } from 'react'
import { Alert, Button, Descriptions, Drawer, Form, Input, Modal, Select, Space, Table, Tabs, Tag, message } from 'antd'
import type { TableColumnsType } from 'antd'
import { PlusOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons'
import { ListToolbar, PageHeader, Panel } from '../components/ui'
import { registryApi, type WatchCategory, type WatchPerson } from '../api/client'
import { useAuth } from '../context/AuthContext'

function apiError(reason: any, fallback: string) {
  return reason?.response?.data?.detail || reason?.message || fallback
}

export default function WatchPeopleManagement() {
  const { user } = useAuth()
  const canManage = Boolean(user?.permissions?.includes('registry.watch.manage'))
  const [tab, setTab] = useState<'people' | 'categories'>('people')
  const [people, setPeople] = useState<WatchPerson[]>([])
  const [categories, setCategories] = useState<WatchCategory[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [personDetail, setPersonDetail] = useState<any>(null)
  const [detailOpen, setDetailOpen] = useState(false)
  const [modal, setModal] = useState<'person' | 'category' | 'assignment' | null>(null)
  const [selectedPerson, setSelectedPerson] = useState<WatchPerson | null>(null)
  const [selectedAssignment, setSelectedAssignment] = useState<any>(null)
  const [selectedCategory, setSelectedCategory] = useState<WatchCategory | null>(null)
  const [form] = Form.useForm()
  const [keyword, setKeyword] = useState('')

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const [peopleResult, categoryResult] = await Promise.all([
        registryApi.watchPeople({ page: 1, page_size: 200 }),
        registryApi.watchCategories(),
      ])
      setPeople(peopleResult.data)
      setCategories(categoryResult.data)
    } catch (reason) {
      setError(apiError(reason, '人员标记读取失败'))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [])

  const openCreate = (kind: NonNullable<typeof modal>) => {
    setSelectedAssignment(null)
    if (kind === 'person') setSelectedPerson(null)
    if (kind === 'category') setSelectedCategory(null)
    form.resetFields()
    if (kind === 'person') form.setFieldsValue({ verification_status: 'unverified', is_temporary: false })
    if (kind === 'category') form.setFieldsValue({ color: '#1677ff', alert_level: 'normal', is_active: true })
    if (kind === 'assignment') form.setFieldsValue({ status: 'active' })
    setModal(kind)
  }

  const openEditCategory = (row: WatchCategory) => {
    setSelectedCategory(row)
    form.resetFields()
    form.setFieldsValue({ code: row.code, name: row.name, parent_id: row.parent_id, color: row.color, alert_level: row.alert_level, is_active: row.is_active })
    setModal('category')
  }

  const openEditPerson = (row: WatchPerson) => {
    setSelectedPerson(row)
    form.resetFields()
    form.setFieldsValue({ name: row.name, identity_number: row.identity_number, verification_status: row.verification_status, status: row.status })
    setModal('person')
  }

  const openEditAssignment = (row: any) => {
    setSelectedAssignment(row)
    form.resetFields()
    form.setFieldsValue({ category_id: row.category_id, valid_from: row.valid_from, valid_to: row.valid_to, released_at: row.released_at, basis: row.basis, status: row.status })
    setModal('assignment')
  }

  const save = async () => {
    const values = await form.validateFields()
    try {
      if (modal === 'person') {
        if (selectedPerson) await registryApi.updateWatchPerson(selectedPerson.id, values)
        else await registryApi.createWatchPerson(values)
      }
      if (modal === 'category') {
        if (selectedCategory) await registryApi.updateWatchCategory(selectedCategory.id, values)
        else await registryApi.createWatchCategory(values)
      }
      if (modal === 'assignment') {
        if (selectedAssignment) await registryApi.updateWatchAssignment(selectedAssignment.id, values)
        else await registryApi.createWatchAssignment({ ...values, person_id: selectedPerson?.id })
      }
      message.success('保存成功')
      setModal(null)
      await load()
      if (selectedPerson && modal === 'assignment') setPersonDetail(await registryApi.watchPerson(selectedPerson.id))
    } catch (reason) {
      message.error(apiError(reason, '保存失败'))
    }
  }

  const openDetail = async (row: WatchPerson) => {
    setSelectedPerson(row)
    setDetailOpen(true)
    try {
      setPersonDetail(await registryApi.watchPerson(row.id))
    } catch (reason) {
      message.error(apiError(reason, '详情读取失败'))
    }
  }

  const personColumns: TableColumnsType<WatchPerson> = [
    { title: '姓名', dataIndex: 'name', width: 150 },
    ...(user?.role === 'super_admin' ? [{ title: '身份证号', dataIndex: 'identity_number', width: 220, render: (value: string) => value || '未登记' }] : []),
    { title: '核实状态', dataIndex: 'verification_status', width: 110 },
    { title: '状态', dataIndex: 'status', width: 100, render: value => <Tag color={value === 'active' ? 'green' : 'default'}>{value === 'active' ? '启用' : '停用'}</Tag> },
    { title: '操作', width: 220, render: (_, row) => <Space><Button size="small" onClick={() => void openDetail(row)}>查看标记</Button>{canManage && <Button size="small" onClick={() => openEditPerson(row)}>编辑</Button>}</Space> },
  ]
  const categoryColumns: TableColumnsType<WatchCategory> = [
    { title: '分类', dataIndex: 'name', width: 180, render: (value, row) => <Tag color={row.color}>{value}</Tag> },
    { title: '代码', dataIndex: 'code', width: 180 },
    { title: '提示级别', dataIndex: 'alert_level', width: 110 },
    { title: '状态', dataIndex: 'is_active', width: 100, render: value => value ? <Tag color="green">启用</Tag> : <Tag>停用</Tag> },
    { title: '说明', dataIndex: 'description', ellipsis: true },
    { title: '操作', width: 90, render: (_: unknown, row: WatchCategory) => canManage && <Button type="link" size="small" onClick={() => openEditCategory(row)}>编辑</Button> },
  ]

  const categoryOptions = useMemo(() => categories.filter(item => item.is_active).map(item => ({ value: item.id, label: item.name })), [categories])
  const normalizedKeyword = keyword.trim().toLocaleLowerCase()
  const visiblePeople = useMemo(() => normalizedKeyword
    ? people.filter(item => `${item.name} ${item.verification_status} ${item.status}`.toLocaleLowerCase().includes(normalizedKeyword))
    : people, [normalizedKeyword, people])
  const visibleCategories = useMemo(() => normalizedKeyword
    ? categories.filter(item => `${item.name} ${item.code} ${item.description || ''}`.toLocaleLowerCase().includes(normalizedKeyword))
    : categories, [categories, normalizedKeyword])

  return (
    <div className="space-y-4">
      <PageHeader
        title="人员标记"
        description="维护重点人员、五失人员、通勤人员等分类；任务只按身份证精确命中，标记不会改变任务完成口径。"
      />
      {error && <Alert type="error" showIcon message={error} />}
      <Panel>
        <Tabs
          activeKey={tab}
          onChange={value => setTab(value as typeof tab)}
          items={[{ key: 'people', label: '人员标记档案' }, { key: 'categories', label: '标记分类' }]}
        />
        <ListToolbar
          filters={<Input allowClear prefix={<SearchOutlined />} value={keyword} onChange={event => setKeyword(event.target.value)} placeholder={tab === 'people' ? '搜索姓名或状态' : '搜索分类、代码或说明'} className="w-full md:w-80" />}
          meta={<span>当前 {tab === 'people' ? visiblePeople.length : visibleCategories.length} 条</span>}
          actions={<><Button icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button>{canManage && <Button type="primary" icon={<PlusOutlined />} onClick={() => openCreate(tab === 'people' ? 'person' : 'category')}>{tab === 'people' ? '新增人员' : '新增分类'}</Button>}</>}
        />
        {tab === 'people' ? <Table rowKey="id" loading={loading} columns={personColumns} dataSource={visiblePeople} pagination={{ defaultPageSize: 20, showSizeChanger: true }} scroll={{ x: 780 }} /> : <Table rowKey="id" loading={loading} columns={categoryColumns} dataSource={visibleCategories} pagination={{ defaultPageSize: 20, showSizeChanger: true }} scroll={{ x: 780 }} />}
      </Panel>

      <Drawer open={detailOpen} width="min(94vw, 620px)" title={selectedPerson ? `${selectedPerson.name}的人员标记` : '人员标记'} onClose={() => setDetailOpen(false)}>
        {personDetail && (
          <div className="space-y-4">
            <Descriptions bordered size="small" column={1} items={[
              { key: 'name', label: '姓名', children: personDetail.name },
              ...(user?.role === 'super_admin' ? [{ key: 'identity', label: '身份证号', children: personDetail.identity_number || '未登记' }] : []),
              { key: 'status', label: '状态', children: personDetail.status },
              { key: 'verify', label: '核实状态', children: personDetail.verification_status },
            ]} />
            <div className="flex items-center justify-between"><h3 className="font-semibold">标记历史</h3>{canManage && <Button type="primary" icon={<PlusOutlined />} onClick={() => openCreate('assignment')}>新增标记</Button>}</div>
            <Table
              rowKey="id"
              size="small"
              pagination={false}
              dataSource={personDetail.assignments || []}
              columns={[
                { title: '分类', dataIndex: 'category_name', render: (value, row) => <Tag color={row.color}>{value}</Tag> },
                { title: '生效时间', dataIndex: 'valid_from' },
                { title: '结束时间', dataIndex: 'valid_to', render: value => value || '持续有效' },
                { title: '状态', dataIndex: 'status' },
                { title: '依据', dataIndex: 'basis', ellipsis: true },
                { title: '操作', width: 110, render: (_: unknown, row: any) => canManage && <Button type="link" size="small" onClick={() => openEditAssignment(row)}>编辑/解除</Button> },
              ]}
            />
          </div>
        )}
      </Drawer>

      <Modal open={Boolean(modal)} title={modal === 'person' ? `${selectedPerson ? '编辑' : '新增'}人员标记档案` : modal === 'category' ? `${selectedCategory ? '编辑' : '新增'}标记分类` : `${selectedAssignment ? '编辑' : '新增'}人员标记`} okText="保存" cancelText="取消" onOk={() => void save()} onCancel={() => setModal(null)}>
        <Form form={form} layout="vertical">
          {modal === 'person' && <>
            <Form.Item name="name" label="姓名" rules={[{ required: true, message: '请输入姓名' }]}><Input /></Form.Item>
            <Form.Item name="identity_number" label="身份证号"><Input /></Form.Item>
            <Form.Item name="verification_status" label="核实状态"><Select options={[{ value: 'unverified', label: '未核实' }, { value: 'verified', label: '已核实' }]} /></Form.Item>
            {selectedPerson && <Form.Item name="status" label="状态"><Select options={[{ value: 'active', label: '启用' }, { value: 'inactive', label: '停用' }]} /></Form.Item>}
          </>}
          {modal === 'category' && <>
            <Form.Item name="code" label="分类代码" rules={[{ required: true, message: '请输入分类代码' }]}><Input /></Form.Item>
            <Form.Item name="name" label="分类名称" rules={[{ required: true, message: '请输入分类名称' }]}><Input /></Form.Item>
            <Form.Item name="color" label="颜色"><Input /></Form.Item>
            <Form.Item name="alert_level" label="提示级别"><Select options={[{ value: 'normal', label: '普通' }, { value: 'notice', label: '提示' }, { value: 'warning', label: '警示' }, { value: 'critical', label: '高风险' }]} /></Form.Item>
            <Form.Item name="is_active" label="状态"><Select options={[{ value: true, label: '启用' }, { value: false, label: '停用' }]} /></Form.Item>
            <Form.Item name="description" label="说明"><Input.TextArea rows={3} /></Form.Item>
          </>}
          {modal === 'assignment' && <>
            <Form.Item name="category_id" label="标记分类" rules={[{ required: true, message: '请选择分类' }]}><Select options={categoryOptions} /></Form.Item>
            <Form.Item name="valid_from" label="生效时间" rules={[{ required: true, message: '请输入生效时间' }]}><Input placeholder="YYYY-MM-DD HH:mm:ss" /></Form.Item>
            <Form.Item name="valid_to" label="结束时间"><Input placeholder="可留空" /></Form.Item>
            {selectedAssignment && <Form.Item name="released_at" label="解除时间"><Input placeholder="可留空" /></Form.Item>}
            {selectedAssignment && <Form.Item name="status" label="状态"><Select options={[{ value: 'active', label: '有效' }, { value: 'released', label: '已解除' }, { value: 'inactive', label: '停用' }]} /></Form.Item>}
            <Form.Item name="basis" label="依据"><Input.TextArea rows={3} /></Form.Item>
          </>}
        </Form>
      </Modal>
    </div>
  )
}
