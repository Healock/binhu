import { useEffect, useMemo, useState } from 'react'
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
import { EditOutlined, PlusOutlined, StopOutlined } from '@ant-design/icons'
import AppTable from '../components/AppTable'
import { PageHeader, Panel } from '../components/ui'
import {
  createPoliceAddress,
  disablePoliceAddress,
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
  const [editing, setEditing] = useState<PoliceAddressEntry | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const load = async () => {
    setLoading(true)
    try {
      const response = await listPoliceAddresses({ keyword })
      setData(response.data)
      setCommunities(response.communities)
      setError('')
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '小区列表读取失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [])

  const communityOptions = useMemo(() => communities.filter(item => item.enabled).map(item => ({
    value: item.id,
    label: item.name,
  })), [communities])

  const openCreate = () => {
    setEditing(null)
    form.setFieldsValue(emptyPayload)
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
      render: value => value === 'apartment' ? '公寓' : value === 'community' ? '居民小区' : '其他',
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
          {item.enabled && (
            <Popconfirm
              title="停用这条地址记录？"
              description="停用后不会参与新批次匹配，历史批次不受影响。"
              onConfirm={async () => {
                await disablePoliceAddress(item.id)
                message.success('已停用')
                await load()
              }}
            >
              <Button type="link" danger icon={<StopOutlined />}>停用</Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ]

  return (
    <div className="app-page min-w-0">
      <PageHeader
        title="小区管理"
        description="维护居民小区、公寓、别名和正式社区；公寓只参与社区匹配，不会自动判定为无需登记"
        extra={<Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新增地址</Button>}
      />
      {error && <Alert type="error" showIcon message={error} />}

      <Panel
        title={`地址记录（${data.length}）`}
        extra={(
          <Space.Compact>
            <Input.Search
              allowClear
              placeholder="搜索名称、地址、社区或别名"
              value={keyword}
              onChange={event => setKeyword(event.target.value)}
              onSearch={() => load()}
              className="w-[320px] max-w-full"
            />
          </Space.Compact>
        )}
        padded={false}
      >
        <AppTable<PoliceAddressEntry>
          rowKey="id"
          columns={columns}
          dataSource={data}
          loading={loading}
          pagination={{ pageSize: 20, showSizeChanger: true }}
          scroll={{ x: 1400 }}
        />
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
            <Select showSearch optionFilterProp="label" options={communityOptions} />
          </Form.Item>
          <div className="grid grid-cols-2 gap-3">
            <Form.Item name="address_type" label="类型">
              <Select options={[
                { value: 'community', label: '居民小区' },
                { value: 'apartment', label: '公寓' },
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
