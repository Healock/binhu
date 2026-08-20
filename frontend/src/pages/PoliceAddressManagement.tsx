import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Tag,
  message,
} from 'antd'
import type { TableColumnsType } from 'antd'
import {
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  PlusOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import AppTable from '../components/AppTable'
import { ListContent, ListToolbar, PageHeader, Panel } from '../components/ui'
import useDebouncedValue from '../hooks/useDebouncedValue'
import {
  createPoliceAddress,
  deletePoliceAddress,
  exportPoliceAddresses,
  listPoliceAddresses,
  updatePoliceAddress,
  type PoliceAddressEntry,
  type PoliceAddressPayload,
  type PoliceCommunityOption,
} from '../api/client'

const emptyPayload: PoliceAddressPayload = {
  name: '',
  detail_address: '',
  address_type: 'community',
  pattern: '',
  community_id: 0,
  aliases: [],
  enabled: true,
}

export default function PoliceAddressManagement() {
  const [form] = Form.useForm<PoliceAddressPayload>()
  const [data, setData] = useState<PoliceAddressEntry[]>([])
  const [communities, setCommunities] = useState<PoliceCommunityOption[]>([])
  const [loading, setLoading] = useState(false)
  const [keyword, setKeyword] = useState('')
  const [keywordFlush, setKeywordFlush] = useState(0)
  const debouncedKeyword = useDebouncedValue(keyword.trim(), 350, keywordFlush)
  const [editing, setEditing] = useState<PoliceAddressEntry | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [communityLocked, setCommunityLocked] = useState(false)
  const [error, setError] = useState('')
  const listRequestId = useRef(0)

  const load = async () => {
    const requestId = ++listRequestId.current
    setLoading(true)
    try {
      const response = await listPoliceAddresses({ keyword: debouncedKeyword })
      if (requestId !== listRequestId.current) return
      setData(response.data)
      setCommunities(response.communities)
      setCommunityLocked(response.community_locked)
      setError('')
    } catch (reason: any) {
      if (requestId === listRequestId.current) setError(reason?.response?.data?.detail || '小区列表读取失败')
    } finally {
      if (requestId === listRequestId.current) setLoading(false)
    }
  }

  useEffect(() => { void load() }, [debouncedKeyword])

  const communityOptions = useMemo(() => communities.filter(item => item.enabled).map(item => ({
    value: item.id,
    label: item.name,
  })), [communities])

  const openCreate = () => {
    setEditing(null)
    form.setFieldsValue({
      ...emptyPayload,
      community_id: communities.length === 1 ? communities[0].id : 0,
    })
    setModalOpen(true)
  }

  const openEdit = (item: PoliceAddressEntry) => {
    setEditing(item)
    form.setFieldsValue({
      name: item.name,
      detail_address: item.detail_address,
      address_type: item.address_type,
      pattern: item.pattern,
      community_id: item.community_id,
      aliases: item.aliases,
      enabled: item.enabled,
    })
    setModalOpen(true)
  }

  const exportRows = async () => {
    setExporting(true)
    try {
      const blob = await exportPoliceAddresses({ keyword })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `小区管理-${new Date().toISOString().slice(0, 10)}.xlsx`
      anchor.click()
      URL.revokeObjectURL(url)
      message.success(`已导出 ${data.length} 条当前可见记录`)
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '导出失败')
    } finally {
      setExporting(false)
    }
  }

  const save = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      if (editing) await updatePoliceAddress(editing.id, values)
      else await createPoliceAddress(values)
      message.success(editing ? '地址记录已更新' : '地址记录已创建')
      setModalOpen(false)
      await load()
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const columns: TableColumnsType<PoliceAddressEntry> = [
    { title: '名称', dataIndex: 'name', width: 220 },
    { title: '正式社区', dataIndex: 'community_name', width: 130 },
    {
      title: '类型', dataIndex: 'address_type', width: 100,
      render: value => value === 'apartment'
        ? '公寓'
        : value === 'construction_dormitory'
          ? '工地宿舍'
          : value === 'community' ? '居民小区' : '其他',
    },
    { title: '详细地址', dataIndex: 'detail_address', width: 320, ellipsis: true },
    { title: '模式', dataIndex: 'pattern', width: 150, ellipsis: true },
    {
      title: '别名', dataIndex: 'aliases', width: 230,
      render: values => (values?.length ? values.map((value: string) => <Tag key={value}>{value}</Tag>) : '-'),
    },
    {
      title: '状态', dataIndex: 'enabled', width: 90,
      render: value => <Tag color={value ? 'success' : 'default'}>{value ? '启用' : '停用'}</Tag>,
    },
    {
      title: '操作', fixed: 'right', width: 150,
      render: (_, item) => (
        <Space>
          <Button type="link" icon={<EditOutlined />} onClick={() => openEdit(item)}>编辑</Button>
          <Popconfirm
            title="删除这条小区记录？"
            description="删除后将不再参与地址匹配，操作记录仍会保留。"
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={async () => {
              try {
                await deletePoliceAddress(item.id)
                message.success('小区记录已删除')
                await load()
              } catch (reason: any) {
                message.error(reason?.response?.data?.detail || '删除失败')
              }
            }}
          >
            <Button type="link" danger icon={<DeleteOutlined />}>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <div className="app-page min-w-0">
      <PageHeader
        title="小区管理"
        description="维护居民小区、工地宿舍、公寓、别名和正式社区；公寓和工地宿舍只参与社区匹配，不会自动判定为无需登记"
      />
      {error && <Alert type="error" showIcon message={error} />}

      <Panel title="地址记录" padded={false}>
        <ListContent inset>
          <ListToolbar
            filters={<Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索名称、地址、社区或别名"
            value={keyword}
            onChange={event => setKeyword(event.target.value)}
            onPressEnter={() => setKeywordFlush(current => current + 1)}
            className="w-[320px] max-w-full"
          />}
            meta={<span>当前筛选 {data.length} 条</span>}
            actions={<><Button icon={<DownloadOutlined />} loading={exporting} onClick={exportRows}>导出 XLSX</Button><Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新增小区</Button></>}
          />
        <AppTable<PoliceAddressEntry>
          rowKey="id"
          columns={columns}
          dataSource={data}
          loading={loading}
          pagination={{ defaultPageSize: 20, showSizeChanger: true }}
          scroll={{ x: 1400 }}
        />
        </ListContent>
      </Panel>

      <Modal
        open={modalOpen}
        title={editing ? '编辑地址' : '新增地址'}
        okText="保存"
        cancelText="取消"
        confirmLoading={saving}
        onOk={save}
        onCancel={() => setModalOpen(false)}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" initialValues={emptyPayload}>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '请填写名称' }]}>
            <Input maxLength={300} />
          </Form.Item>
          <Form.Item name="community_id" label="正式社区" rules={[{ required: true, message: '请选择社区' }]}>
            <Select
              showSearch
              optionFilterProp="label"
              options={communityOptions}
              disabled={communityLocked}
            />
          </Form.Item>
          <div className="grid grid-cols-2 gap-3">
            <Form.Item name="address_type" label="类型">
              <Select options={[
                { value: 'community', label: '居民小区' },
                { value: 'apartment', label: '公寓' },
                { value: 'construction_dormitory', label: '工地宿舍' },
                { value: 'other', label: '其他' },
              ]} />
            </Form.Item>
            <Form.Item name="enabled" label="启用" valuePropName="checked">
              <Switch />
            </Form.Item>
          </div>
          <Form.Item name="detail_address" label="详细地址">
            <Input.TextArea rows={2} maxLength={1000} />
          </Form.Item>
          <Form.Item name="pattern" label="模式">
            <Input maxLength={200} placeholder="例如：开放式" />
          </Form.Item>
          <Form.Item name="aliases" label="别名">
            <Select mode="tags" tokenSeparators={[',', '，', '、']} maxCount={50} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
