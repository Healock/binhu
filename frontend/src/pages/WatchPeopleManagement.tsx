import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert, Button, DatePicker, Descriptions, Drawer, Form, Input, Modal, Select, Space,
  Table, Tabs, Tag, Upload, message,
} from 'antd'
import type { TableColumnsType, UploadFile } from 'antd'
import { InboxOutlined, PlusOutlined, ReloadOutlined, SearchOutlined, UploadOutlined } from '@ant-design/icons'
import { ListContent, ListToolbar, PageHeader, Panel } from '../components/ui'
import useDebouncedValue from '../hooks/useDebouncedValue'
import {
  registryApi,
  type WatchCategory,
  type WatchImportPreview,
  type WatchPerson,
} from '../api/client'
import { useAuth } from '../context/AuthContext'
import dayjs from 'dayjs'

const CORE_CATEGORY_CODES = ['通勤人员', '五失人员', '重点人员', '精障人员']

function apiError(reason: any, fallback: string) {
  return reason?.response?.data?.detail || reason?.message || fallback
}

export default function WatchPeopleManagement() {
  const { user } = useAuth()
  const canManage = Boolean(user?.permissions?.includes('registry.watch.manage'))
  const canImport = user?.role === 'super_admin'
    && Boolean(user?.permissions?.includes('registry.import.manage'))
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
  const [keywordInput, setKeywordInput] = useState('')
  const [keywordFlush, setKeywordFlush] = useState(0)
  const keyword = useDebouncedValue(keywordInput.trim(), 350, keywordFlush)
  const [categoryIds, setCategoryIds] = useState<number[]>([])
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)
  const [total, setTotal] = useState(0)
  const [reloadToken, setReloadToken] = useState(0)
  const listRequestId = useRef(0)

  const [importOpen, setImportOpen] = useState(false)
  const [importFiles, setImportFiles] = useState<UploadFile[]>([])
  const [importCategory, setImportCategory] = useState('通勤人员')
  const [importPreview, setImportPreview] = useState<WatchImportPreview | null>(null)
  const [importing, setImporting] = useState(false)

  const loadCategories = useCallback(async () => {
    try {
      const result = await registryApi.watchCategories()
      setCategories(result.data)
    } catch (reason) {
      setError(apiError(reason, '人员标签分类读取失败'))
    }
  }, [])

  useEffect(() => { void loadCategories() }, [loadCategories])

  useEffect(() => {
    if (tab !== 'people') return
    const requestId = ++listRequestId.current
    setLoading(true)
    setError('')
    void registryApi.searchWatchPeople({ keyword, category_ids: categoryIds, page, page_size: pageSize })
      .then(result => {
        if (requestId !== listRequestId.current) return
        setPeople(result.data)
        setTotal(result.total)
      })
      .catch(reason => {
        if (requestId === listRequestId.current) setError(apiError(reason, '人员标签读取失败'))
      })
      .finally(() => {
        if (requestId === listRequestId.current) setLoading(false)
      })
  }, [categoryIds, keyword, page, pageSize, reloadToken, tab])

  const refresh = () => {
    setReloadToken(value => value + 1)
    void loadCategories()
  }

  const openCreate = (kind: NonNullable<typeof modal>) => {
    setSelectedAssignment(null)
    if (kind === 'person') setSelectedPerson(null)
    if (kind === 'category') setSelectedCategory(null)
    form.resetFields()
    if (kind === 'person') form.setFieldsValue({ verification_status: 'unverified', is_temporary: false })
    if (kind === 'category') form.setFieldsValue({ color: '#1677ff', alert_level: 'normal', is_active: true })
    if (kind === 'assignment') form.setFieldsValue({ status: 'active', valid_from: dayjs() })
    setModal(kind)
  }

  const openEditCategory = (row: WatchCategory) => {
    setSelectedCategory(row)
    form.resetFields()
    form.setFieldsValue({
      code: row.code, name: row.name, parent_id: row.parent_id, color: row.color,
      alert_level: row.alert_level, is_active: row.is_active, description: row.description,
    })
    setModal('category')
  }

  const openEditPerson = (row: WatchPerson) => {
    setSelectedPerson(row)
    form.resetFields()
    form.setFieldsValue({
      name: row.name, identity_number: row.identity_number,
      verification_status: row.verification_status, status: row.status,
    })
    setModal('person')
  }

  const openEditAssignment = (row: any) => {
    setSelectedAssignment(row)
    form.resetFields()
    form.setFieldsValue({
      category_id: row.category_id, valid_from: row.valid_from ? dayjs(row.valid_from) : null, valid_to: row.valid_to ? dayjs(row.valid_to) : null,
      released_at: row.released_at ? dayjs(row.released_at) : null, basis: row.basis, status: row.status,
    })
    setModal('assignment')
  }

  const save = async () => {
    const values = await form.validateFields()
    for (const field of ['valid_from', 'valid_to', 'released_at']) {
      if (values[field]?.format) values[field] = values[field].format('YYYY-MM-DD HH:mm:ss')
    }
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
      refresh()
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

  const openImport = () => {
    setImportFiles([])
    setImportCategory('通勤人员')
    setImportPreview(null)
    setImportOpen(true)
  }

  const previewImport = async () => {
    const files = importFiles.map(item => item.originFileObj).filter(Boolean) as File[]
    if (!files.length) return
    setImporting(true)
    try {
      const result = await registryApi.previewWatchImport(files, importCategory)
      setImportPreview(result)
      if (result.blocking_count) message.warning('预览完成，名单存在需要先处理的阻断问题')
      else message.success('预览完成，尚未写入人员标签库')
    } catch (reason) {
      message.error(apiError(reason, '人员标签名单预览失败'))
    } finally {
      setImporting(false)
    }
  }

  const confirmImport = async () => {
    if (!importPreview?.can_confirm || importPreview.status === 'imported') return
    setImporting(true)
    try {
      const result = await registryApi.confirmWatchImport(importPreview.batch_id)
      message.success(`导入完成：${result.created_people || 0} 个新人员，${result.created_assignments || 0} 条新标签`)
      setImportOpen(false)
      refresh()
    } catch (reason) {
      message.error(apiError(reason, '人员标签名单导入失败'))
    } finally {
      setImporting(false)
    }
  }

  const personColumns: TableColumnsType<WatchPerson> = [
    { title: '姓名', dataIndex: 'name', width: 130 },
    ...(user?.role === 'super_admin'
      ? [{ title: '身份证号', dataIndex: 'identity_number', width: 210, render: (value: string) => value || '未登记' }]
      : []),
    {
      title: '人员标签', dataIndex: 'categories', minWidth: 250,
      render: (values: WatchPerson['categories']) => values?.length
        ? <Space size={[4, 4]} wrap>{values.map(item => <Tag key={item.id} color={item.color}>{item.name}</Tag>)}</Space>
        : <span className="text-slate-400">暂无有效标签</span>,
    },
    { title: '核实状态', dataIndex: 'verification_status', width: 110 },
    {
      title: '状态', dataIndex: 'status', width: 90,
      render: value => <Tag color={value === 'active' ? 'green' : 'default'}>{value === 'active' ? '启用' : '停用'}</Tag>,
    },
    {
      title: '操作', width: 190,
      render: (_, row) => <Space>
        <Button size="small" onClick={() => void openDetail(row)}>查看标签</Button>
        {canManage && <Button size="small" onClick={() => openEditPerson(row)}>编辑</Button>}
      </Space>,
    },
  ]
  const categoryColumns: TableColumnsType<WatchCategory> = [
    { title: '分类', dataIndex: 'name', width: 180, render: (value, row) => <Tag color={row.color}>{value}</Tag> },
    { title: '代码', dataIndex: 'code', width: 180 },
    { title: '提示级别', dataIndex: 'alert_level', width: 110 },
    { title: '状态', dataIndex: 'is_active', width: 100, render: value => value ? <Tag color="green">启用</Tag> : <Tag>停用</Tag> },
    { title: '说明', dataIndex: 'description', ellipsis: true },
    { title: '操作', width: 90, render: (_: unknown, row: WatchCategory) => canManage && <Button type="link" size="small" onClick={() => openEditCategory(row)}>编辑</Button> },
  ]

  const categoryOptions = useMemo(
    () => categories.filter(item => item.is_active).map(item => ({ value: item.id, label: item.name })),
    [categories],
  )
  const importCategoryOptions = useMemo(() => CORE_CATEGORY_CODES.map(code => ({ value: code, label: code })), [])
  const normalizedCategoryKeyword = keywordInput.trim().toLocaleLowerCase()
  const visibleCategories = useMemo(() => normalizedCategoryKeyword
    ? categories.filter(item => `${item.name} ${item.code} ${item.description || ''}`.toLocaleLowerCase().includes(normalizedCategoryKeyword))
    : categories, [categories, normalizedCategoryKeyword])

  const importIssues = importPreview ? [
    ['身份证为空', importPreview.missing_identity_count],
    ['身份证格式或校验位异常', importPreview.invalid_identity_count],
    ['姓名为空', importPreview.missing_name_count],
    ['同一身份证姓名不一致', importPreview.name_conflict_groups],
    ['同一身份证手机号不一致', importPreview.phone_conflict_groups],
    ['与现有档案姓名不一致', importPreview.existing_name_conflict_count],
    ['现有人员档案已停用', importPreview.inactive_people_count],
  ].filter(([, count]) => Number(count) > 0) : []

  return (
    <div className="space-y-4">
      <PageHeader
        title="人员标签"
        description="维护通勤人员、五失人员、重点人员和精障人员数据库；任务按身份证精确命中，标签只用于识别与筛选，不改变任务完成口径。"
      />
      {error && <Alert type="error" showIcon message={error} />}
      <Panel>
        <Tabs
          activeKey={tab}
          onChange={value => {
            setTab(value as typeof tab)
            setKeywordInput('')
            setCategoryIds([])
            setPage(1)
          }}
          items={[{ key: 'people', label: '人员标签库' }, { key: 'categories', label: '标签分类' }]}
        />
        <ListContent>
          <ListToolbar
          notice={tab === 'people' ? <Alert
            type="info"
            showIcon
            message="四类特殊人员统一维护"
            description="当前核心分类为通勤人员、五失人员、重点人员、精障人员。同一人员可以拥有多个标签，同一分类只保留一条当前有效标签。"
          /> : undefined}
          filters={<>
            <Input
              allowClear
              prefix={<SearchOutlined />}
              value={keywordInput}
              onChange={event => { setKeywordInput(event.target.value); setPage(1) }}
              onPressEnter={() => setKeywordFlush(value => value + 1)}
              placeholder={tab === 'people' ? '搜索姓名；超管可输入完整身份证号' : '搜索分类、代码或说明'}
              className="w-full md:w-80"
            />
            {tab === 'people' && <Select
              mode="multiple"
              allowClear
              value={categoryIds}
              onChange={value => { setCategoryIds(value); setPage(1) }}
              options={categoryOptions}
              maxTagCount="responsive"
              placeholder="筛选人员标签"
              className="w-full md:w-72"
            />}
          </>}
          meta={<span>当前筛选共 {tab === 'people' ? total : visibleCategories.length} 条</span>}
          actions={<>
            <Button icon={<ReloadOutlined />} onClick={refresh}>刷新</Button>
            {canImport && tab === 'people' && <Button icon={<UploadOutlined />} onClick={openImport}>批量导入</Button>}
            {canManage && <Button type="primary" icon={<PlusOutlined />} onClick={() => openCreate(tab === 'people' ? 'person' : 'category')}>
              {tab === 'people' ? '新增人员' : '新增分类'}
            </Button>}
          </>}
          />
          {tab === 'people' ? <Table
          rowKey="id"
          loading={loading}
          columns={personColumns}
          dataSource={people}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            pageSizeOptions: [20, 50, 100, 200],
            showTotal: count => `共 ${count} 条`,
            onChange: (nextPage, nextPageSize) => {
              setPage(nextPageSize === pageSize ? nextPage : 1)
              setPageSize(nextPageSize)
            },
          }}
          scroll={{ x: 900 }}
          /> : <Table
          rowKey="id"
          loading={loading}
          columns={categoryColumns}
          dataSource={visibleCategories}
          pagination={{ defaultPageSize: 20, showSizeChanger: true }}
          scroll={{ x: 780 }}
          />}
        </ListContent>
      </Panel>

      <Drawer open={detailOpen} width="min(94vw, 620px)" title={selectedPerson ? `${selectedPerson.name}的人员标签` : '人员标签'} onClose={() => setDetailOpen(false)}>
        {personDetail && <div className="watch-person-detail">
          <section className="watch-person-detail__section"><Descriptions bordered size="small" column={1} items={[
            { key: 'name', label: '姓名', children: personDetail.name },
            ...(user?.role === 'super_admin' ? [{ key: 'identity', label: '身份证号', children: personDetail.identity_number || '未登记' }] : []),
            { key: 'status', label: '状态', children: personDetail.status },
            { key: 'verify', label: '核实状态', children: personDetail.verification_status },
          ]} /></section>
          <section className="watch-person-detail__section"><div className="flex items-center justify-between gap-3"><h3 className="font-semibold">标签历史</h3>{canManage && <Button type="primary" icon={<PlusOutlined />} onClick={() => openCreate('assignment')}>新增标签</Button>}</div>
          <Table rowKey="id" size="small" pagination={false} dataSource={personDetail.assignments || []} columns={[
            { title: '分类', dataIndex: 'category_name', render: (value, row) => <Tag color={row.color}>{value}</Tag> },
            { title: '生效时间', dataIndex: 'valid_from' },
            { title: '结束时间', dataIndex: 'valid_to', render: value => value || '持续有效' },
            { title: '状态', dataIndex: 'status' },
            { title: '依据', dataIndex: 'basis', ellipsis: true },
            { title: '操作', width: 110, render: (_: unknown, row: any) => canManage && <Button type="link" size="small" onClick={() => openEditAssignment(row)}>编辑/解除</Button> },
          ]} /></section>
        </div>}
      </Drawer>

      <Modal open={Boolean(modal)} title={modal === 'person' ? `${selectedPerson ? '编辑' : '新增'}人员标签档案` : modal === 'category' ? `${selectedCategory ? '编辑' : '新增'}标签分类` : `${selectedAssignment ? '编辑' : '新增'}人员标签`} okText="保存" cancelText="取消" onOk={() => void save()} onCancel={() => setModal(null)}>
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
            <Form.Item name="category_id" label="标签分类" rules={[{ required: true, message: '请选择分类' }]}><Select options={categoryOptions} /></Form.Item>
            <Form.Item name="valid_from" label="生效时间" rules={[{ required: true, message: '请选择生效时间' }]}><DatePicker showTime format="YYYY-MM-DD HH:mm:ss" className="w-full" /></Form.Item>
            <Form.Item name="valid_to" label="结束时间"><DatePicker showTime format="YYYY-MM-DD HH:mm:ss" className="w-full" /></Form.Item>
            {selectedAssignment && <Form.Item name="released_at" label="解除时间"><DatePicker showTime format="YYYY-MM-DD HH:mm:ss" className="w-full" /></Form.Item>}
            {selectedAssignment && <Form.Item name="status" label="状态"><Select options={[{ value: 'active', label: '有效' }, { value: 'released', label: '已解除' }, { value: 'inactive', label: '停用' }]} /></Form.Item>}
            <Form.Item name="basis" label="依据"><Input.TextArea rows={3} /></Form.Item>
          </>}
        </Form>
      </Modal>

      <Modal
        open={importOpen}
        title="批量导入人员标签"
        width="min(94vw, 760px)"
        okText={importPreview?.status === 'imported' ? '已导入' : '确认导入'}
        cancelText="关闭"
        okButtonProps={{ disabled: !importPreview?.can_confirm || importPreview?.status === 'imported' }}
        confirmLoading={importing}
        onOk={() => void confirmImport()}
        onCancel={() => setImportOpen(false)}
      >
        <div className="space-y-4">
          <Alert type="info" showIcon message="先预览，再确认写入" description="支持同时选择多个 .xls/.xlsx 名单。系统按身份证号合并同一人员；重复来源行会保留来源记录，但不会重复创建人员或同类标签。" />
          <Select value={importCategory} options={importCategoryOptions} onChange={value => { setImportCategory(value); setImportPreview(null) }} className="w-full" aria-label="导入标签分类" />
          <Upload.Dragger multiple accept=".xls,.xlsx" fileList={importFiles} beforeUpload={() => false} onChange={({ fileList }) => { setImportFiles(fileList); setImportPreview(null) }}>
            <p className="ant-upload-drag-icon"><InboxOutlined /></p>
            <p className="ant-upload-text">点击或拖入人员名单</p>
            <p className="ant-upload-hint">可一次选择多份表格，合计不超过 50MB</p>
          </Upload.Dragger>
          <Button type="primary" icon={<SearchOutlined />} loading={importing} disabled={!importFiles.length} onClick={() => void previewImport()}>预览导入</Button>
          {importPreview && <>
            <Descriptions bordered size="small" column={{ xs: 1, sm: 2 }} items={[
              { key: 'files', label: '文件数', children: importPreview.file_count },
              { key: 'rows', label: '来源行', children: importPreview.total_rows },
              { key: 'people', label: '唯一人员', children: importPreview.unique_people },
              { key: 'duplicate', label: '重复来源行', children: importPreview.duplicate_rows },
              { key: 'new_people', label: '新增人员', children: importPreview.new_people },
              { key: 'existing_people', label: '已有人员', children: importPreview.existing_people },
              { key: 'new_assignments', label: '新增标签', children: importPreview.new_assignments },
              { key: 'existing_assignments', label: '已有标签', children: importPreview.existing_assignments },
              { key: 'blocking', label: '阻断问题', children: importPreview.blocking_count },
              { key: 'state', label: '批次状态', children: importPreview.status === 'imported' ? '已导入' : '仅预览' },
            ]} />
            {importIssues.length > 0 && <Alert type="error" showIcon message="名单暂不能确认导入" description={<Space direction="vertical" size={2}>{importIssues.map(([label, count]) => <span key={String(label)}>{label}：{count} 条/组</span>)}</Space>} />}
            {!importPreview.blocking_count && importPreview.status !== 'imported' && <Alert type="success" showIcon message="预览通过，可以确认导入" description="确认后才会写入正式人员标签库。没有社区字段的名单不会猜测社区；当前在线任务会在后续正常同步时补齐标签快照。" />}
          </>}
        </div>
      </Modal>
    </div>
  )
}
