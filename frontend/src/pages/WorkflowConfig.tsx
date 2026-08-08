import { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Divider,
  Drawer,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  message,
} from 'antd'
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  DeleteOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import { PageHeader, Panel } from '../components/ui'
import { workflowApi, type WorkflowType } from '../api/client'

function apiError(reason: any, fallback: string) {
  return reason?.response?.data?.detail || reason?.message || fallback
}

const QUEUES = ['基础管控', '中队长', '组长', '组员', '社区民警', '所队领导']
const FIELD_TYPES = [
  { value: 'text', label: '单行文字' },
  { value: 'textarea', label: '多行文字' },
  { value: 'date', label: '日期' },
  { value: 'datetime', label: '日期时间' },
  { value: 'select', label: '下拉选项' },
  { value: 'number', label: '数字' },
]

interface WorkflowVersionSummary {
  id: number
  version_no: number
  status: string
  approval_mode: string
  published_at: string | null
  created_at: string
}

export default function WorkflowConfig() {
  const [types, setTypes] = useState<WorkflowType[]>([])
  const [versions, setVersions] = useState<WorkflowVersionSummary[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [typeOpen, setTypeOpen] = useState(false)
  const [configOpen, setConfigOpen] = useState(false)
  const [activeType, setActiveType] = useState<WorkflowType | null>(null)
  const [editingVersionId, setEditingVersionId] = useState<number | null>(null)
  const [editingPublished, setEditingPublished] = useState(false)
  const [saving, setSaving] = useState(false)
  const [typeForm] = Form.useForm()
  const [versionForm] = Form.useForm()

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      setTypes((await workflowApi.types()).data)
    } catch (reason) {
      setError(apiError(reason, '流程读取失败'))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [])

  const createType = async () => {
    try {
      const values = await typeForm.validateFields()
      await workflowApi.createType({ ...values, form_schema: { fields: [] } })
      message.success('工单类型已创建')
      setTypeOpen(false)
      await load()
    } catch (reason: any) {
      if (!reason?.errorFields) message.error(apiError(reason, '创建失败'))
    }
  }

  const newVersionDefaults = (type: WorkflowType) => ({
    approval_mode: 'sequential',
    fields: (type.form_schema?.fields || []).map((field: any) => ({
      ...field,
      options_text: Array.isArray(field.options) ? field.options.join('\n') : '',
    })),
    steps: [{
      name: type.code === 'photo_request' ? '基础管控处理' : '审批节点',
      step_type: type.code === 'photo_request' ? 'handling' : 'approval',
      queue: '基础管控',
      claim_required: true,
      due_hours: type.default_due_hours || 24,
      reminder_before_minutes: 60,
      allow_transfer: true,
    }],
  })

  const openConfig = async (type: WorkflowType) => {
    setActiveType(type)
    setEditingVersionId(null)
    setEditingPublished(false)
    versionForm.setFieldsValue(newVersionDefaults(type))
    setConfigOpen(true)
    try {
      setVersions((await workflowApi.versions(type.id)).data || [])
    } catch (reason) {
      message.error(apiError(reason, '流程版本读取失败'))
    }
  }

  const loadVersion = async (versionId: number) => {
    try {
      const version = await workflowApi.version(versionId)
      setEditingVersionId(version.id)
      setEditingPublished(version.status !== 'draft')
      versionForm.setFieldsValue({
        approval_mode: version.approval_mode || 'sequential',
        fields: (version.form_schema?.fields || []).map((field: any) => ({
          ...field,
          options_text: Array.isArray(field.options) ? field.options.join('\n') : '',
        })),
        steps: (version.steps || []).map((step: any) => ({
          name: step.name,
          step_type: step.step_type,
          queue: step.queue,
          claim_required: step.claim_required !== false,
          due_hours: step.due_hours || null,
          reminder_before_minutes: step.reminder_before_minutes || null,
          allow_transfer: step.allow_transfer !== false,
        })),
      })
    } catch (reason) {
      message.error(apiError(reason, '流程版本读取失败'))
    }
  }

  const resetNewVersion = () => {
    if (!activeType) return
    setEditingVersionId(null)
    setEditingPublished(false)
    versionForm.setFieldsValue(newVersionDefaults(activeType))
  }

  const saveVersion = async (publish: boolean) => {
    if (!activeType) return
    try {
      const values = await versionForm.validateFields()
      setSaving(true)
      const formSchema = {
        fields: (values.fields || []).map((field: any) => ({
          name: field.name,
          label: field.label,
          type: field.type,
          required: Boolean(field.required),
          ...(field.type === 'select'
            ? { options: String(field.options_text || '').split(/\r?\n/).map((item: string) => item.trim()).filter(Boolean) }
            : {}),
        })),
      }
      const payload = {
        form_schema: formSchema,
        approval_mode: 'sequential',
        steps: (values.steps || []).map((step: any) => ({
          name: step.name,
          step_type: step.step_type,
          queue: step.queue,
          claim_required: Boolean(step.claim_required),
          due_hours: step.due_hours || null,
          reminder_before_minutes: step.reminder_before_minutes || null,
          allow_transfer: Boolean(step.allow_transfer),
        })),
      }
      let versionId = editingVersionId
      if (versionId) {
        await workflowApi.updateVersion(versionId, payload)
      } else {
        const result = await workflowApi.createVersion(activeType.id, payload)
        versionId = result.id
      }
      if (publish && versionId) await workflowApi.publishVersion(versionId)
      message.success(publish ? '流程版本已发布' : '流程草稿已保存')
      setVersions((await workflowApi.versions(activeType.id)).data || [])
      if (publish) resetNewVersion()
      else if (versionId) await loadVersion(versionId)
      await load()
    } catch (reason: any) {
      if (!reason?.errorFields) message.error(apiError(reason, publish ? '流程发布失败' : '流程保存失败'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="工单流程配置"
        description="超级管理员可以配置表单字段和顺序处理节点。已发布版本保持只读，新工单使用最新发布版本，旧工单继续按原版本执行。"
        actions={(
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => { typeForm.resetFields(); setTypeOpen(true) }}>新增类型</Button>
          </Space>
        )}
      />
      {error && <Alert type="error" showIcon message={error} />}
      <Panel>
        <Table
          rowKey="id"
          loading={loading}
          dataSource={types}
          pagination={false}
          columns={[
            { title: '名称', dataIndex: 'name' },
            { title: '代码', dataIndex: 'code' },
            { title: '状态', dataIndex: 'enabled', render: value => value ? <Tag color="green">已启用</Tag> : <Tag>未发布</Tag> },
            { title: '默认时限', dataIndex: 'default_due_hours', render: value => value ? `${value} 小时` : '未设置' },
            { title: '操作', render: (_, row) => <Button size="small" onClick={() => void openConfig(row)}>流程版本</Button> },
          ]}
        />
      </Panel>
      <Card size="small" title="配置原则">
        <p className="text-sm text-[var(--app-text-secondary)]">第一版按节点顺序流转；“处理节点”和“审批节点”都可以进入岗位队列领取。请假流程保持未发布，直到审批链由超级管理员确认。</p>
      </Card>

      <Modal open={typeOpen} title="新增工单类型" okText="保存" cancelText="取消" onOk={() => void createType()} onCancel={() => setTypeOpen(false)}>
        <Form form={typeForm} layout="vertical">
          <Form.Item name="code" label="代码" rules={[{ required: true }, { pattern: /^[a-z][a-z0-9_]*$/, message: '只能使用小写字母、数字和下划线，并以字母开头' }]}><Input /></Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="description" label="说明"><Input.TextArea maxLength={1000} /></Form.Item>
          <Form.Item name="default_due_hours" label="默认处理时限（小时）"><InputNumber min={1} max={8760} className="w-full" /></Form.Item>
        </Form>
      </Modal>

      <Drawer
        open={configOpen}
        width="min(98vw, 1080px)"
        title={`${activeType?.name || ''} · 流程版本`}
        onClose={() => setConfigOpen(false)}
        extra={<Button onClick={resetNewVersion}>新建草稿</Button>}
      >
        <div className="grid gap-5 xl:grid-cols-[300px_1fr]">
          <Card size="small" title="历史版本">
            <Table
              size="small"
              rowKey="id"
              pagination={false}
              dataSource={versions}
              columns={[
                { title: '版本', dataIndex: 'version_no', render: value => `v${value}` },
                { title: '状态', dataIndex: 'status', render: value => value === 'published' ? <Tag color="green">已发布</Tag> : <Tag color="blue">草稿</Tag> },
                { title: '', render: (_, row) => <Button type="link" size="small" onClick={() => void loadVersion(row.id)}>{row.status === 'draft' ? '编辑' : '查看'}</Button> },
              ]}
            />
          </Card>

          <Form form={versionForm} layout="vertical" disabled={editingPublished}>
            {editingPublished && <Alert className="mb-4" type="info" showIcon message="已发布版本只读；如需调整，请新建流程草稿。" />}
            <Form.Item name="approval_mode" hidden><Input /></Form.Item>

            <Card size="small" title="表单字段">
              <Form.List name="fields">
                {(fields, { add, remove }) => (
                  <div className="space-y-3">
                    {fields.map(field => (
                      <div key={field.key} className="grid gap-3 rounded-lg border border-[var(--app-border)] p-3 md:grid-cols-[1fr_1fr_160px_90px_auto]">
                        <Form.Item {...field} name={[field.name, 'name']} label="字段代码" rules={[{ required: true }]}><Input /></Form.Item>
                        <Form.Item {...field} name={[field.name, 'label']} label="显示名称" rules={[{ required: true }]}><Input /></Form.Item>
                        <Form.Item {...field} name={[field.name, 'type']} label="类型" rules={[{ required: true }]}><Select options={FIELD_TYPES} /></Form.Item>
                        <Form.Item {...field} name={[field.name, 'required']} label="必填" valuePropName="checked"><Checkbox /></Form.Item>
                        <Button className="mt-8" danger type="text" icon={<DeleteOutlined />} onClick={() => remove(field.name)} />
                        <Form.Item noStyle shouldUpdate>
                          {({ getFieldValue }) => getFieldValue(['fields', field.name, 'type']) === 'select' && (
                            <Form.Item className="md:col-span-5" {...field} name={[field.name, 'options_text']} label="下拉选项（每行一个）" rules={[{ required: true }]}>
                              <Input.TextArea rows={3} />
                            </Form.Item>
                          )}
                        </Form.Item>
                      </div>
                    ))}
                    <Button type="dashed" block icon={<PlusOutlined />} onClick={() => add({ type: 'text', required: false })}>添加表单字段</Button>
                  </div>
                )}
              </Form.List>
            </Card>

            <Divider />

            <Card size="small" title="顺序节点">
              <Form.List name="steps" rules={[{ validator: async (_, steps) => { if (!steps?.length) throw new Error('至少需要一个流程节点') } }]}>
                {(fields, { add, remove, move }, { errors }) => (
                  <div className="space-y-3">
                    {fields.map((field, index) => (
                      <div key={field.key} className="rounded-lg border border-[var(--app-border)] p-4">
                        <div className="mb-3 flex items-center justify-between">
                          <strong>第 {index + 1} 节点</strong>
                          <Space>
                            <Button size="small" icon={<ArrowUpOutlined />} disabled={index === 0} onClick={() => move(index, index - 1)} />
                            <Button size="small" icon={<ArrowDownOutlined />} disabled={index === fields.length - 1} onClick={() => move(index, index + 1)} />
                            <Popconfirm title="删除这个节点？" onConfirm={() => remove(field.name)}><Button size="small" danger icon={<DeleteOutlined />} /></Popconfirm>
                          </Space>
                        </div>
                        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                          <Form.Item {...field} name={[field.name, 'name']} label="节点名称" rules={[{ required: true }]}><Input /></Form.Item>
                          <Form.Item {...field} name={[field.name, 'step_type']} label="节点类型" rules={[{ required: true }]}><Select options={[{ value: 'approval', label: '审批节点' }, { value: 'handling', label: '处理节点' }]} /></Form.Item>
                          <Form.Item {...field} name={[field.name, 'queue']} label="岗位队列" rules={[{ required: true }]}><Select showSearch options={QUEUES.map(value => ({ value, label: value }))} /></Form.Item>
                          <Form.Item {...field} name={[field.name, 'due_hours']} label="处理时限（小时）"><InputNumber min={1} max={8760} className="w-full" /></Form.Item>
                          <Form.Item {...field} name={[field.name, 'reminder_before_minutes']} label="提前提醒（分钟）"><InputNumber min={5} max={43200} className="w-full" /></Form.Item>
                          <div className="flex items-center gap-5 pt-7">
                            <Form.Item {...field} name={[field.name, 'claim_required']} valuePropName="checked"><Checkbox>需要领取</Checkbox></Form.Item>
                            <Form.Item {...field} name={[field.name, 'allow_transfer']} valuePropName="checked"><Checkbox>允许转派</Checkbox></Form.Item>
                          </div>
                        </div>
                      </div>
                    ))}
                    <Form.ErrorList errors={errors} />
                    <Button type="dashed" block icon={<PlusOutlined />} onClick={() => add({ name: '处理节点', step_type: 'handling', queue: '基础管控', claim_required: true, due_hours: 24, reminder_before_minutes: 60, allow_transfer: true })}>添加流程节点</Button>
                  </div>
                )}
              </Form.List>
            </Card>

            {!editingPublished && (
              <div className="mt-5 flex justify-end gap-3">
                <Button loading={saving} onClick={() => void saveVersion(false)}>保存草稿</Button>
                <Popconfirm title="发布后该版本将不能修改，确认发布？" onConfirm={() => void saveVersion(true)}>
                  <Button type="primary" loading={saving}>保存并发布</Button>
                </Popconfirm>
              </div>
            )}
          </Form>
        </div>
      </Drawer>
    </div>
  )
}
