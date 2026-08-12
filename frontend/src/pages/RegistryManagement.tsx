import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert, Button, Descriptions, Drawer, Form, Input, Modal, Popconfirm,
  Select, Space, Tabs, Tag, message,
} from 'antd'
import type { TableColumnsType } from 'antd'
import { PlusOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons'
import AppTable from '../components/AppTable'
import { ListToolbar, PageHeader, Panel } from '../components/ui'
import useDebouncedValue from '../hooks/useDebouncedValue'
import {
  formatUTCTime,
  getGridCommunities,
  registryApi,
  type RegistryOrganization,
  type RegistryPerson,
  type RegistryProperty,
} from '../api/client'
import { useAuth } from '../context/AuthContext'

type TabKey = 'properties' | 'people' | 'organizations' | 'merges' | 'candidates' | 'conflicts'
type ModalKind = 'property' | 'person' | 'organization' | 'phone' | 'alias' | 'personRelation' | 'organizationRelation' | 'merge'

export default function RegistryManagement() {
  const { user, systemTimezone } = useAuth()
  const [tab, setTab] = useState<TabKey>('properties')
  const [properties, setProperties] = useState<RegistryProperty[]>([])
  const [people, setPeople] = useState<RegistryPerson[]>([])
  const [organizations, setOrganizations] = useState<RegistryOrganization[]>([])
  const [merges, setMerges] = useState<any[]>([])
  const [candidates, setCandidates] = useState<any[]>([])
  const [conflicts, setConflicts] = useState<any[]>([])
  const [communities, setCommunities] = useState<Array<{ id: number; name: string }>>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [keyword, setKeyword] = useState('')
  const [keywordFlush, setKeywordFlush] = useState(0)
  const debouncedKeyword = useDebouncedValue(keyword.trim(), 350, keywordFlush)
  const [modal, setModal] = useState<ModalKind | null>(null)
  const [selected, setSelected] = useState<any>(null)
  const [detailKind, setDetailKind] = useState<'property' | 'person' | 'organization' | null>(null)
  const [roleTypes, setRoleTypes] = useState<Array<{ id: number; name: string; subject_type: 'person' | 'organization'; is_active: boolean }>>([])
  const [detail, setDetail] = useState<any>(null)
  const [detailOpen, setDetailOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const listRequestId = useRef(0)
  const [form] = Form.useForm()
  const canManage = user?.permissions?.includes('registry.property.manage')
  const canReview = user?.permissions?.includes('registry.import.manage')

  const load = async () => {
    const requestId = ++listRequestId.current
    setLoading(true)
    try {
      if (tab === 'properties') setProperties((await registryApi.properties()).data)
      if (tab === 'people') {
        const response = debouncedKeyword
          ? await registryApi.searchPeople({ name: debouncedKeyword, page: 1, page_size: 100 })
          : await registryApi.people({ page_size: 100 })
        setPeople(response.data)
      }
      if (tab === 'organizations') setOrganizations((await registryApi.organizations({ keyword: debouncedKeyword, page_size: 100 })).data)
      if (tab === 'merges') setMerges((await registryApi.mergeHistory({ page_size: 100 })).data || [])
      if (tab === 'candidates') setCandidates((await registryApi.candidates()).data || [])
      if (tab === 'conflicts') setConflicts((await registryApi.conflicts()).data || [])
      if (requestId !== listRequestId.current) return
      setError('')
    } catch (reason: any) {
      if (requestId === listRequestId.current) setError(reason?.response?.data?.detail || '辖区档案读取失败')
    } finally {
      if (requestId === listRequestId.current) setLoading(false)
    }
  }

  useEffect(() => {
    void getGridCommunities().then(items => setCommunities(items.filter(item => item.is_active))).catch(() => undefined)
    void registryApi.roleTypes().then(response => setRoleTypes(response.data.filter(item => item.is_active))).catch(() => undefined)
  }, [])
  useEffect(() => { void load() }, [tab, debouncedKeyword])

  const currentCount = tab === 'properties' ? properties.length
    : tab === 'people' ? people.length
      : tab === 'organizations' ? organizations.length
        : tab === 'merges' ? merges.length
          : tab === 'candidates' ? candidates.length
            : conflicts.length

  const communityOptions = useMemo(() => communities.map(item => ({ value: item.id, label: item.name })), [communities])

  const openCreate = (kind: ModalKind) => {
    setSelected(null)
    form.resetFields()
    if (kind === 'property') form.setFieldsValue({ status: 'active' })
    if (kind === 'person') form.setFieldsValue({ verification_status: 'unverified', is_temporary: false })
    if (kind === 'organization') form.setFieldsValue({ organization_type: 'other' })
    setModal(kind)
  }

  const openEdit = (kind: 'property' | 'person' | 'organization', row: any) => {
    setSelected(row)
    form.resetFields()
    if (kind === 'property') {
      form.setFieldsValue({
        street: row.street, community_id: row.community_id, natural_address: row.natural_address,
        building: row.building, room: row.room, normalized_address: row.normalized_address,
        change_reason: '',
      })
    } else if (kind === 'person') {
      form.setFieldsValue({ name: row.name, identity_number: row.identity_number, verification_status: row.verification_status, is_temporary: row.is_temporary })
    } else {
      form.setFieldsValue({ name: row.name, organization_type: row.organization_type, license_number: row.license_number, notes: row.notes })
    }
    setModal(kind)
  }

  const openDetail = async (kind: 'property' | 'person' | 'organization', row: any) => {
    setSelected(row)
    setDetailKind(kind)
    setDetailOpen(true)
    setDetail(null)
    try {
      setDetail(kind === 'property' ? await registryApi.property(row.id) : kind === 'person' ? await registryApi.person(row.id) : await registryApi.organization(row.id))
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '档案详情读取失败')
    }
  }

  const save = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      if (modal === 'property') {
        const community = communities.find(item => item.id === values.community_id)
        const payload = { ...values, community_name_snapshot: community?.name || selected?.community_name || '' }
        if (selected) await registryApi.updateProperty(selected.id, payload)
        else await registryApi.createProperty(payload)
      } else if (modal === 'person') {
        if (selected) await registryApi.updatePerson(selected.id, values)
        else await registryApi.createPerson(values)
      } else if (modal === 'organization') {
        if (selected) await registryApi.updateOrganization(selected.id, values)
        else await registryApi.createOrganization(values)
      } else if (modal === 'phone' && selected) {
        await registryApi.addPhone(selected.id, values)
      } else if (modal === 'alias' && selected) {
        await registryApi.addPropertyAlias(selected.id, values)
      } else if (modal === 'personRelation' && selected) {
        await registryApi.addPropertyPersonRelation(selected.id, values)
      } else if (modal === 'organizationRelation' && selected) {
        await registryApi.addPropertyOrganizationRelation(selected.id, values)
      } else if (modal === 'merge' && selected) {
        await registryApi.mergePerson(selected.id, values)
      }
      message.success('保存成功')
      setModal(null)
      await load()
      if (detailOpen && selected && detailKind === 'property' && ['alias', 'personRelation', 'organizationRelation'].includes(modal || '')) setDetail(await registryApi.property(selected.id))
      if (detailOpen && selected && detailKind === 'person' && modal === 'phone') setDetail(await registryApi.person(selected.id))
      if (detailOpen && selected && detailKind === 'organization' && modal === 'organizationRelation') setDetail(await registryApi.organization(selected.id))
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const reviewCandidate = async (id: number, action: 'accept' | 'reject') => {
    await registryApi.reviewCandidate(id, { action, reason: action === 'accept' ? '采用候选变更' : '不采用候选变更' })
    message.success('候选变更已处理')
    await load()
  }

  const reviewConflict = async (id: number, action: 'accept' | 'reject') => {
    await registryApi.reviewConflict(id, { action, reason: action === 'accept' ? '按审核结果解决' : '忽略该冲突' })
    message.success('冲突已处理')
    await load()
  }

  const endRelation = async (kind: 'person' | 'organization' | 'membership', relation: any) => {
    const payload = { valid_from: relation.valid_from || null, valid_to: new Date().toISOString(), verified: Boolean(relation.verified), ...(kind === 'membership' ? { title: relation.title || '' } : {}) }
    if (kind === 'person') await registryApi.updatePropertyPersonRelation(relation.relation_id, payload)
    if (kind === 'organization') await registryApi.updatePropertyOrganizationRelation(relation.relation_id, payload)
    if (kind === 'membership') await registryApi.updateOrganizationMembership(relation.membership_id, payload)
    message.success('关系已结束，历史记录保留')
    if (detailKind === 'property' && selected) setDetail(await registryApi.property(selected.id))
    if (detailKind === 'organization' && selected) setDetail(await registryApi.organization(selected.id))
  }

  const toggleProperty = async (row: RegistryProperty) => {
    await registryApi.changePropertyStatus(row.id, { status: row.status === 'active' ? 'inactive' : 'active', reason: '人工维护' })
    message.success(row.status === 'active' ? '房屋已停用' : '房屋已启用')
    await load()
  }

  const propertyColumns: TableColumnsType<RegistryProperty> = [
    { title: '社区', dataIndex: 'community_name', width: 130 },
    { title: '标准化地址', dataIndex: 'normalized_address', width: 340, ellipsis: true },
    { title: '幢', dataIndex: 'building', width: 90 },
    { title: '室', dataIndex: 'room', width: 90 },
    { title: '状态', dataIndex: 'status', width: 90, render: value => <Tag color={value === 'active' ? 'green' : 'default'}>{value === 'active' ? '启用' : '停用'}</Tag> },
    { title: '版本', dataIndex: 'version', width: 80 },
    { title: '操作', key: 'actions', width: 210, render: (_, row) => <Space>
      <Button size="small" onClick={() => openDetail('property', row)}>详情</Button>
      {canManage && <Button size="small" onClick={() => openEdit('property', row)}>编辑</Button>}
      {canManage && <Popconfirm title={row.status === 'active' ? '确认停用这套房屋？' : '确认启用这套房屋？'} onConfirm={() => void toggleProperty(row)}><Button size="small">{row.status === 'active' ? '停用' : '启用'}</Button></Popconfirm>}
    </Space> },
  ]
  const personColumns: TableColumnsType<RegistryPerson> = [
    { title: '姓名', dataIndex: 'name', width: 130 },
    ...(user?.role === 'super_admin' ? [{ title: '身份证号', dataIndex: 'identity_number', width: 210, render: (value: string) => value || '未登记' }] : []),
    { title: '核实状态', dataIndex: 'verification_status', width: 110 },
    { title: '临时档案', dataIndex: 'is_temporary', width: 100, render: value => value ? <Tag color="gold">是</Tag> : '否' },
    { title: '状态', dataIndex: 'status', width: 90 },
    { title: '操作', key: 'actions', width: 250, render: (_, row) => <Space>
      <Button size="small" onClick={() => openDetail('person', row)}>详情</Button>
      {canManage && <Button size="small" onClick={() => openEdit('person', row)}>编辑</Button>}
      {canManage && <Button size="small" onClick={() => { setSelected(row); form.resetFields(); setModal('merge') }}>合并</Button>}
    </Space> },
  ]
  const organizationColumns: TableColumnsType<RegistryOrganization> = [
    { title: '机构名称', dataIndex: 'name', width: 220 },
    { title: '类型', dataIndex: 'organization_type', width: 130 },
    { title: '登记编号', dataIndex: 'license_number', width: 180 },
    { title: '备注', dataIndex: 'notes', ellipsis: true },
    { title: '状态', dataIndex: 'status', width: 90 },
    { title: '操作', key: 'actions', width: 120, render: (_, row) => <Space><Button size="small" onClick={() => openDetail('organization', row)}>详情</Button>{canManage && <Button size="small" onClick={() => openEdit('organization', row)}>编辑</Button>}</Space> },
  ]

  return (
    <div className="space-y-4">
      <PageHeader title="辖区档案" description="长期维护辖区房屋、房东、业主、中介和租房平台关系；业务数据只进入待审核变更。" />
      {error && <Alert type="error" showIcon message={error} />}
      <Panel>
        <Tabs
          activeKey={tab}
          onChange={value => setTab(value as TabKey)}
          items={[
            { key: 'properties', label: '房屋档案' },
            { key: 'people', label: '人员档案' },
            { key: 'organizations', label: '机构档案' },
            ...(canManage ? [{ key: 'merges', label: '合并历史' }] : []),
            ...(canReview ? [{ key: 'candidates', label: '待审核变更' }, { key: 'conflicts', label: '冲突处理' }] : []),
          ]}
        />
        <ListToolbar
          filters={<Input
            allowClear
            prefix={<SearchOutlined />}
            value={keyword}
            onChange={event => setKeyword(event.target.value)}
            onPressEnter={() => setKeywordFlush(current => current + 1)}
            placeholder="搜索姓名或机构名称"
            disabled={!['people', 'organizations'].includes(tab)}
            className="w-full md:w-80"
          />}
          meta={<span>当前 {currentCount} 条</span>}
          actions={<>
            <Button icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button>
            {canManage && tab === 'properties' && <Button type="primary" icon={<PlusOutlined />} onClick={() => openCreate('property')}>新增房屋</Button>}
            {canManage && tab === 'people' && <Button type="primary" icon={<PlusOutlined />} onClick={() => openCreate('person')}>新增人员</Button>}
            {canManage && tab === 'organizations' && <Button type="primary" icon={<PlusOutlined />} onClick={() => openCreate('organization')}>新增机构</Button>}
          </>}
        />
        {tab === 'properties' && <AppTable rowKey="id" loading={loading} columns={propertyColumns} dataSource={properties} pagination={false} scroll={{ x: 950 }} />}
        {tab === 'people' && <AppTable rowKey="id" loading={loading} columns={personColumns} dataSource={people} pagination={false} scroll={{ x: 850 }} />}
        {tab === 'organizations' && <AppTable rowKey="id" loading={loading} columns={organizationColumns} dataSource={organizations} pagination={false} scroll={{ x: 850 }} />}
        {tab === 'merges' && <AppTable rowKey="id" loading={loading} dataSource={merges} pagination={false} columns={[
          { title: '源档案', render: (_: unknown, row: any) => `${row.source_name}（${row.source_person_id}）` },
          { title: '目标档案', render: (_: unknown, row: any) => `${row.target_name}（${row.target_person_id}）` },
          { title: '原因', dataIndex: 'reason', ellipsis: true },
          { title: '时间', dataIndex: 'created_at', width: 180, render: value => formatUTCTime(value, systemTimezone) },
          { title: '状态', dataIndex: 'undone', width: 90, render: (value: boolean) => value ? <Tag>已撤销</Tag> : <Tag color="orange">已合并</Tag> },
          { title: '操作', width: 110, render: (_: unknown, row: any) => !row.undone && <Popconfirm title="确认撤销这次合并？" onConfirm={async () => { await registryApi.undoMerge(row.id); message.success('合并已撤销'); await load() }}><Button size="small">撤销</Button></Popconfirm> },
        ]} />}
        {tab === 'candidates' && <AppTable rowKey="id" loading={loading} dataSource={candidates} pagination={false} columns={[
          { title: '对象', dataIndex: 'entity_type', width: 120 },
          { title: '变更', dataIndex: 'change_type', width: 120 },
          { title: '原因', dataIndex: 'reason', ellipsis: true },
          { title: '创建时间', dataIndex: 'created_at', width: 180, render: value => formatUTCTime(value, systemTimezone) },
          { title: '操作', width: 170, render: (_: unknown, row: any) => <Space><Button size="small" type="primary" onClick={() => void reviewCandidate(row.id, 'accept')}>采用</Button><Button size="small" danger onClick={() => void reviewCandidate(row.id, 'reject')}>拒绝</Button></Space> },
        ]} />}
        {tab === 'conflicts' && <AppTable rowKey="id" loading={loading} dataSource={conflicts} pagination={false} columns={[
          { title: '对象', dataIndex: 'entity_type', width: 120 },
          { title: '冲突类型', dataIndex: 'conflict_type', width: 160 },
          { title: '对象键', dataIndex: 'entity_key', ellipsis: true },
          { title: '创建时间', dataIndex: 'created_at', width: 180, render: value => formatUTCTime(value, systemTimezone) },
          { title: '操作', width: 170, render: (_: unknown, row: any) => <Space><Button size="small" type="primary" onClick={() => void reviewConflict(row.id, 'accept')}>解决</Button><Button size="small" onClick={() => void reviewConflict(row.id, 'reject')}>忽略</Button></Space> },
        ]} />}
      </Panel>

      <Modal open={Boolean(modal)} title={modal === 'property' ? `${selected ? '编辑' : '新增'}房屋档案` : modal === 'person' ? `${selected ? '编辑' : '新增'}辖区人员` : modal === 'organization' ? `${selected ? '编辑' : '新增'}机构档案` : modal === 'phone' ? '添加联系电话' : modal === 'alias' ? '添加地址别名' : modal === 'personRelation' ? '添加房屋人员关系' : modal === 'organizationRelation' ? '添加房屋机构关系' : '合并人员档案'} onCancel={() => setModal(null)} onOk={() => void save()} confirmLoading={saving} destroyOnClose>
        <Form form={form} layout="vertical" preserve={false}>
          {modal === 'property' && <>
            <Form.Item name="community_id" label="社区" rules={[{ required: true }]}><Select showSearch options={communityOptions} /></Form.Item>
            <Form.Item name="street" label="街道"><Input /></Form.Item>
            <Form.Item name="natural_address" label="自然地址" rules={[{ required: true }]}><Input /></Form.Item>
            <div className="grid grid-cols-2 gap-3"><Form.Item name="building" label="幢"><Input /></Form.Item><Form.Item name="room" label="室"><Input /></Form.Item></div>
            <Form.Item name="normalized_address" label="标准化地址"><Input.TextArea autoSize /></Form.Item>
            {selected && <Form.Item name="change_reason" label="变更原因"><Input.TextArea autoSize /></Form.Item>}
          </>}
          {modal === 'person' && <>
            <Form.Item name="name" label="姓名" rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item name="identity_number" label="身份证号"><Input /></Form.Item>
            <Form.Item name="verification_status" label="核实状态"><Select options={[{ value: 'unverified', label: '未核实' }, { value: 'pending', label: '待核实' }, { value: 'verified', label: '已核实' }]} /></Form.Item>
          </>}
          {modal === 'organization' && <>
            <Form.Item name="name" label="机构名称" rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item name="organization_type" label="机构类型"><Select options={[{ value: 'agency', label: '中介' }, { value: 'rental_platform', label: '租房平台' }, { value: 'property', label: '物业' }, { value: 'other', label: '其他' }]} /></Form.Item>
            <Form.Item name="license_number" label="登记编号"><Input /></Form.Item>
            <Form.Item name="notes" label="备注"><Input.TextArea autoSize /></Form.Item>
          </>}
          {modal === 'phone' && <>
            <Form.Item name="phone" label="手机号" rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item name="is_primary" label="主号码" initialValue={false}><Select options={[{ value: false, label: '否' }, { value: true, label: '是' }]} /></Form.Item>
          </>}
          {modal === 'alias' && <Form.Item name="alias" label="地址别名" rules={[{ required: true }]}><Input /></Form.Item>}
          {modal === 'personRelation' && <>
            <Form.Item name="person_id" label="人员" rules={[{ required: true }]}><Select showSearch options={people.filter(item => item.status === 'active').map(item => ({ value: item.id, label: item.name }))} /></Form.Item>
            <Form.Item name="role_type_id" label="角色" rules={[{ required: true }]}><Select options={roleTypes.filter(item => item.subject_type === 'person').map(item => ({ value: item.id, label: item.name }))} /></Form.Item>
            <Form.Item name="valid_from" label="生效时间"><Input placeholder="留空表示即时生效" /></Form.Item>
          </>}
          {modal === 'organizationRelation' && <>
            <Form.Item name="organization_id" label="机构" rules={[{ required: true }]}><Select showSearch options={organizations.filter(item => item.status === 'active').map(item => ({ value: item.id, label: item.name }))} /></Form.Item>
            <Form.Item name="role_type_id" label="角色" rules={[{ required: true }]}><Select options={roleTypes.filter(item => item.subject_type === 'organization').map(item => ({ value: item.id, label: item.name }))} /></Form.Item>
            <Form.Item name="valid_from" label="生效时间"><Input placeholder="留空表示即时生效" /></Form.Item>
          </>}
          {modal === 'merge' && <>
            <Alert type="warning" showIcon message="只建议合并确认属于同一人的档案；身份证号不一致时后端会拒绝。" className="mb-3" />
            <Form.Item name="target_person_id" label="合并到目标档案" rules={[{ required: true }]}><Select showSearch options={people.filter(item => item.id !== selected?.id && item.status === 'active').map(item => ({ value: item.id, label: `${item.name}（${item.id}）` }))} /></Form.Item>
            <Form.Item name="reason" label="合并原因"><Input.TextArea autoSize /></Form.Item>
          </>}
        </Form>
      </Modal>

      <Drawer open={detailOpen} onClose={() => setDetailOpen(false)} width="min(94vw, 760px)" title={detail?.normalized_address || detail?.name || '档案详情'}>
        {!detail ? <div className="py-16 text-center text-[var(--app-text-secondary)]">正在读取…</div> : <div className="space-y-5">
          {canManage && detailKind === 'property' && <Space wrap>
            <Button onClick={() => openEdit('property', detail)}>编辑房屋</Button>
            <Button icon={<PlusOutlined />} onClick={() => { setSelected(detail); form.resetFields(); setModal('alias') }}>添加地址别名</Button>
            <Button onClick={() => { setSelected(detail); form.resetFields(); setModal('personRelation') }}>添加人员关系</Button>
            <Button onClick={() => { setSelected(detail); form.resetFields(); setModal('organizationRelation') }}>添加机构关系</Button>
          </Space>}
          {canManage && detailKind === 'person' && <Space><Button onClick={() => openEdit('person', detail)}>编辑人员</Button><Button onClick={() => { setSelected(detail); form.resetFields(); setModal('phone') }}>添加号码</Button></Space>}
          {canManage && detailKind === 'organization' && <Space><Button onClick={() => openEdit('organization', detail)}>编辑机构</Button></Space>}
          <Descriptions bordered size="small" column={1} items={Object.entries(detail).filter(([key, value]) => !Array.isArray(value) && !['identity_hmac'].includes(key)).slice(0, 12).map(([key, value]) => ({ key, label: key, children: String(value ?? '-') }))} />
          {detail.aliases && <Panel title="地址别名"><Space wrap>{detail.aliases.length ? detail.aliases.map((item: any) => <Tag key={item.id} color={item.enabled ? 'blue' : undefined}>{item.alias}{item.enabled ? '' : ' · 已停用'}{canManage && <Button type="link" size="small" onClick={async () => { await registryApi.changeAliasStatus(item.id, { status: item.enabled ? 'inactive' : 'active' }); setDetail(await registryApi.property(detail.id)) }}>{item.enabled ? '停用' : '启用'}</Button>}</Tag>) : '暂无'}</Space></Panel>}
          {detail.phones && <Panel title="联系电话" extra={canManage ? <Button size="small" icon={<PlusOutlined />} onClick={() => { form.resetFields(); setModal('phone') }}>添加号码</Button> : undefined}>
            <Space wrap>{detail.phones.length ? detail.phones.map((item: any) => <Tag key={item.id} color={item.is_primary ? 'blue' : undefined}>{item.phone}{item.is_primary ? ' · 主号码' : ''}</Tag>) : '暂无'}</Space>
          </Panel>}
          {detail.people && <Panel title="房屋人员关系"><AppTable rowKey="relation_id" pagination={false} dataSource={detail.people} columns={[{ title: '姓名', dataIndex: 'person_name' }, { title: '角色', dataIndex: 'role_name' }, { title: '生效', dataIndex: 'valid_from' }, { title: '结束', dataIndex: 'valid_to' }, { title: '操作', render: (_: unknown, row: any) => canManage && !row.valid_to && <Popconfirm title="结束该关系？" onConfirm={() => void endRelation('person', row)}><Button type="link" size="small">结束</Button></Popconfirm> }]} /></Panel>}
          {detail.organizations && <Panel title="房屋机构关系"><AppTable rowKey="relation_id" pagination={false} dataSource={detail.organizations} columns={[{ title: '机构', dataIndex: 'organization_name' }, { title: '角色', dataIndex: 'role_name' }, { title: '生效', dataIndex: 'valid_from' }, { title: '结束', dataIndex: 'valid_to' }, { title: '操作', render: (_: unknown, row: any) => canManage && !row.valid_to && <Popconfirm title="结束该关系？" onConfirm={() => void endRelation('organization', row)}><Button type="link" size="small">结束</Button></Popconfirm> }]} /></Panel>}
          {detail.members && <Panel title="机构经办人"><AppTable rowKey="membership_id" pagination={false} dataSource={detail.members} columns={[{ title: '姓名', dataIndex: 'person_name' }, { title: '职位', dataIndex: 'title' }, { title: '生效', dataIndex: 'valid_from' }, { title: '结束', dataIndex: 'valid_to' }, { title: '操作', render: (_: unknown, row: any) => canManage && !row.valid_to && <Popconfirm title="结束该任职关系？" onConfirm={() => void endRelation('membership', row)}><Button type="link" size="small">结束</Button></Popconfirm> }]} /></Panel>}
          {detail.properties && <Panel title="机构关联房屋"><AppTable rowKey="relation_id" pagination={false} dataSource={detail.properties} columns={[{ title: '地址', dataIndex: 'normalized_address' }, { title: '角色', dataIndex: 'role_name' }, { title: '生效', dataIndex: 'valid_from' }, { title: '结束', dataIndex: 'valid_to' }]} /></Panel>}
          {detail.versions && <Panel title="地址版本历史"><AppTable rowKey="version" pagination={false} dataSource={detail.versions} columns={[{ title: '版本', dataIndex: 'version', width: 80 }, { title: '标准化地址', dataIndex: 'normalized_address' }, { title: '变更原因', dataIndex: 'reason' }, { title: '时间', dataIndex: 'created_at', width: 180, render: value => formatUTCTime(value, systemTimezone) }]} /></Panel>}
        </div>}
      </Drawer>
    </div>
  )
}
