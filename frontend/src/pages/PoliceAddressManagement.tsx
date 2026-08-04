import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Radio,
  Select,
  Space,
  Switch,
  Tag,
  Upload,
  message,
} from 'antd'
import type { TableColumnsType, UploadFile, UploadProps } from 'antd'
import { EditOutlined, InboxOutlined, PlusOutlined, StopOutlined } from '@ant-design/icons'
import AppTable from '../components/AppTable'
import { PageHeader, Panel } from '../components/ui'
import {
  createPoliceAddress,
  disablePoliceAddress,
  importPoliceAddresses,
  listPoliceAddresses,
  updatePoliceAddress,
  type PoliceAddressEntry,
  type PoliceAddressImportResult,
  type PoliceAddressPayload,
  type PoliceCommunityOption,
} from '../api/client'

const { Dragger } = Upload

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
  const [importKind, setImportKind] = useState<'community' | 'apartment'>('community')
  const [importFile, setImportFile] = useState<File | null>(null)
  const [importFiles, setImportFiles] = useState<UploadFile[]>([])
  const [importing, setImporting] = useState(false)
  const [preview, setPreview] = useState<PoliceAddressImportResult | null>(null)
  const [error, setError] = useState('')

  const load = async () => {
    setLoading(true)
    try {
      const response = await listPoliceAddresses({ keyword })
      setData(response.data)
      setCommunities(response.communities)
      setError('')
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '公安地址库读取失败')
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

  const beforeImport: UploadProps['beforeUpload'] = file => {
    const suffix = file.name.toLowerCase()
    if (!suffix.endsWith('.xls') && !suffix.endsWith('.xlsx')) {
      message.error('只支持 .xls 或 .xlsx 文件')
      return Upload.LIST_IGNORE
    }
    setImportFile(file)
    setImportFiles([{
      uid: file.uid,
      name: file.name,
      size: file.size,
      status: 'done',
      originFileObj: file,
    }])
    setPreview(null)
    return false
  }

  const runImport = async (commit: boolean) => {
    if (!importFile) return
    setImporting(true)
    try {
      const result = await importPoliceAddresses(importFile, importKind, commit)
      setPreview(result)
      if (commit) {
        message.success(result.status === 'duplicate' ? '同一文件已经导入' : '地址映射已入库')
        await load()
      }
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '映射表处理失败')
    } finally {
      setImporting(false)
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
        title="小区管理（公安地址库）"
        description="统一维护居民小区、公寓、别名和正式社区；公寓只参与社区映射，不会自动判定为无需登记"
        extra={<Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新增地址</Button>}
      />
      {error && <Alert type="error" showIcon message={error} />}

      <Panel title="重新导入映射表" description="先预览冲突；空白社区自动继承上一项，未识别社区不会写入">
        <div className="grid gap-4 lg:grid-cols-[220px_minmax(0,1fr)_auto] lg:items-end">
          <div>
            <div className="mb-2 text-sm font-medium text-slate-700">映射表类型</div>
            <Radio.Group
              value={importKind}
              onChange={event => { setImportKind(event.target.value); setPreview(null) }}
              optionType="button"
              buttonStyle="solid"
              options={[
                { label: '各小区', value: 'community' },
                { label: '滨湖公寓明细', value: 'apartment' },
              ]}
            />
          </div>
          <Dragger
            accept=".xls,.xlsx"
            maxCount={1}
            fileList={importFiles}
            beforeUpload={beforeImport}
            onRemove={() => { setImportFile(null); setImportFiles([]); setPreview(null) }}
          >
            <p className="ant-upload-drag-icon"><InboxOutlined /></p>
            <p className="ant-upload-text">选择公安地址映射表</p>
          </Dragger>
          <Space wrap>
            <Button disabled={!importFile} loading={importing} onClick={() => runImport(false)}>预览</Button>
            <Button
              type="primary"
              disabled={!importFile || preview?.status !== 'preview'}
              loading={importing}
              onClick={() => runImport(true)}
            >
              确认入库
            </Button>
          </Space>
        </div>
        {preview && (
          <div className="mt-4 space-y-3">
            <Alert
              type={(preview.conflicts?.length || 0) > 0 ? 'warning' : 'success'}
              showIcon
              message={preview.status === 'duplicate' ? '文件已导入' : `识别 ${preview.total || 0} 条映射`}
              description={`新增 ${preview.created_count ?? preview.create_count ?? 0} 条，合并 ${preview.merged_count ?? preview.merge_count ?? 0} 条，冲突 ${preview.conflicts?.length || preview.conflict_count || 0} 条。冲突不会写入。`}
            />
            {Boolean(preview.conflicts?.length) && (
              <div className="overflow-hidden rounded-xl border border-amber-200 bg-amber-50/60">
                <div className="border-b border-amber-200 px-4 py-2 text-sm font-medium text-amber-900">
                  冲突明细
                </div>
                <div className="max-h-72 divide-y divide-amber-100 overflow-y-auto">
                  {preview.conflicts?.map((item, index) => (
                    <div key={`${item.source_row || index}-${item.name || ''}`} className="grid gap-1 px-4 py-3 text-sm md:grid-cols-[90px_1fr_140px_2fr]">
                      <span className="text-amber-700">第 {item.source_row || '-'} 行</span>
                      <span className="font-medium text-slate-800">{item.name || '-'}</span>
                      <span className="text-slate-600">{item.community_text || '-'}</span>
                      <span className="text-amber-800">{item.reason || '需要人工处理'}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </Panel>

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
        title={editing ? '编辑公安地址' : '新增公安地址'}
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
