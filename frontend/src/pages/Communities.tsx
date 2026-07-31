import { useState, useEffect, useCallback } from 'react'
import { Alert, Button, Input, Modal, Select, Tag } from 'antd'
import type { TableColumnsType } from 'antd'
import { EditOutlined, PlusOutlined } from '@ant-design/icons'
import {
  getGridCommunities,
  addGridCommunity,
  deleteGridCommunity,
  updateGridCommunityDetails,
  type GridCommunity,
} from '../api/client'
import AppTable from '../components/AppTable'
import { EmptyState, LoadingState, PageHeader } from '../components/ui'
import { useAuth } from '../context/AuthContext'

export default function Communities() {
  const { user } = useAuth()
  const canManage = Boolean(user?.permissions.includes('community.manage'))
  const [communities, setCommunities] = useState<GridCommunity[]>([])
  const [newName, setNewName] = useState('')
  const [msg, setMsg] = useState('')
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [editingCommunity, setEditingCommunity] = useState<GridCommunity | null>(null)
  const [nameDraft, setNameDraft] = useState('')
  const [aliasDraft, setAliasDraft] = useState<string[]>([])
  const [officerDraft, setOfficerDraft] = useState<string[]>([])
  const [savingDetails, setSavingDetails] = useState(false)

  const fetch = useCallback(async () => {
    setLoading(true)
    setLoadError('')
    try {
      setCommunities(await getGridCommunities())
    } catch {
      setLoadError('社区列表加载失败，请稍后重试')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetch() }, [fetch])

  const handleAdd = async () => {
    if (!newName.trim()) return
    try { await addGridCommunity(newName.trim()); setNewName(''); setMsg('添加成功'); fetch() }
    catch (e: any) { setMsg(e?.response?.data?.detail || '添加失败') }
  }

  const handleDelete = (id: number, name: string) => {
    Modal.confirm({
      title: '删除社区',
      content: `确认删除社区“${name}”？`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await deleteGridCommunity(id)
          setMsg(`已删除社区“${name}”`)
          fetch()
        } catch {
          setMsg('删除失败，请稍后重试')
        }
      },
    })
  }

  const openCommunityEditor = (community: GridCommunity) => {
    setEditingCommunity(community)
    setNameDraft(community.name)
    setAliasDraft(community.aliases || [])
    setOfficerDraft(community.police_officers || [])
  }

  const handleSaveDetails = async () => {
    if (!editingCommunity) return
    setSavingDetails(true)
    try {
      const result = await updateGridCommunityDetails(
        editingCommunity.id,
        nameDraft.trim(),
        aliasDraft,
        officerDraft,
      )
      const matchedText = result.matched_visit_rows > 0
        ? `，同时归类 ${result.matched_visit_rows} 条已有走访数据`
        : ''
      setMsg(`“${editingCommunity.name}”的社区资料已保存${matchedText}`)
      setEditingCommunity(null)
      setNameDraft('')
      setAliasDraft([])
      setOfficerDraft([])
      await fetch()
    } catch (error: any) {
      setMsg(`保存失败：${error?.response?.data?.detail || '请稍后重试'}`)
    } finally {
      setSavingDetails(false)
    }
  }

  const communityColumns: TableColumnsType<GridCommunity> = [
    {
      title: '社区名称',
      dataIndex: 'name',
      key: 'name',
      width: 240,
      sorter: (left, right) => left.name.localeCompare(right.name, 'zh-CN'),
      render: value => <span className="font-medium text-slate-800">{value}</span>,
    },
    {
      title: '人员数量',
      dataIndex: 'grid_count',
      key: 'grid_count',
      width: 160,
      sorter: (left, right) => left.grid_count - right.grid_count,
    },
    {
      title: '社区民警',
      dataIndex: 'police_officers',
      key: 'police_officers',
      width: 260,
      render: officers => officers?.length > 0
        ? <span>{officers.join('、')}</span>
        : <span className="text-slate-400">暂未填写</span>,
    },
    {
      title: '别名',
      dataIndex: 'aliases',
      key: 'aliases',
      width: 300,
      render: aliases => aliases?.length > 0
        ? (
          <div className="flex flex-wrap gap-1">
            {aliases.map(alias => <Tag key={alias}>{alias}</Tag>)}
          </div>
        )
        : <span className="text-slate-400">暂无别名</span>,
    },
    ...(canManage ? [{
      title: '操作',
      key: 'actions',
      width: 190,
      render: (_, community) => (
        <div className="flex items-center gap-1">
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={() => openCommunityEditor(community)}
          >
            编辑资料
          </Button>
          <Button type="link" danger size="small" onClick={() => handleDelete(community.id, community.name)}>
            删除
          </Button>
        </div>
      ),
    }] : []),
  ]

  return (
    <div className="app-page">
      <PageHeader
        title="社区管理"
        description="维护社区名单、别名和社区民警，工作日志会自动读取这里的信息"
        actions={<Tag color="blue">共 {communities.length} 个社区</Tag>}
      />

      {canManage && <section className="app-card">
        <div className="app-toolbar">
          <Input
            value={newName}
            onChange={event => setNewName(event.target.value)}
            onPressEnter={handleAdd}
            placeholder="输入社区名称"
            className="min-w-56 flex-1"
          />
          <Button type="primary" icon={<PlusOutlined />} onClick={handleAdd} disabled={!newName.trim()}>
            添加社区
          </Button>
        </div>
        {msg && <Alert type={msg.includes('失败') ? 'error' : 'success'} showIcon message={msg} />}
      </section>}

      {loading ? (
        <div className="app-table-wrap">
          <LoadingState />
        </div>
      ) : loadError ? (
        <div className="app-table-wrap">
          <EmptyState label={loadError} />
        </div>
      ) : communities.length === 0 ? (
        <div className="app-table-wrap">
          <EmptyState label="暂无社区，可在上方输入社区名称后添加" />
        </div>
      ) : (
        <>
          <div className="app-table-wrap md:hidden">
            <div className="grid grid-cols-1 gap-3 p-4">
              {communities.map((c) => (
                <div key={c.id} className="border border-gray-200 rounded-lg p-4 flex items-center justify-between">
                  <div>
                    <div className="font-medium text-gray-800">{c.name}</div>
                    <div className="text-sm text-gray-500">人员 {c.grid_count} 人</div>
                    <div className="mt-1 text-sm text-slate-600">
                      社区民警：{c.police_officers?.length > 0
                        ? c.police_officers.join('、')
                        : '暂未填写'}
                    </div>
                    <div className="mt-2 flex flex-wrap gap-1">
                      {c.aliases?.length > 0
                        ? c.aliases.map(alias => <Tag key={alias}>{alias}</Tag>)
                        : <span className="text-xs text-slate-400">暂无别名</span>}
                    </div>
                  </div>
                  {canManage && <div className="flex flex-col items-end">
                    <Button type="link" size="small" onClick={() => openCommunityEditor(c)}>编辑资料</Button>
                    <Button type="link" danger size="small" onClick={() => handleDelete(c.id, c.name)}>删除</Button>
                  </div>}
                </div>
              ))}
            </div>
          </div>
          <div className="hidden md:block">
            <AppTable<GridCommunity>
              columns={communityColumns}
              dataSource={communities}
              rowKey="id"
              scroll={{ x: 900 }}
            />
          </div>
        </>
      )}
      <p className="text-xs text-slate-500">人员数量会根据“人员管理”中的社区部门自动统计，无需手动填写。</p>

      {canManage && <Modal
        open={Boolean(editingCommunity)}
        title={editingCommunity ? `编辑“${editingCommunity.name}”` : '编辑社区资料'}
        okText="保存"
        cancelText="取消"
        confirmLoading={savingDetails}
        onOk={handleSaveDetails}
        onCancel={() => {
          setEditingCommunity(null)
          setNameDraft('')
          setAliasDraft([])
          setOfficerDraft([])
        }}
      >
        <div className="space-y-5">
          <div>
            <div className="mb-2 font-medium text-slate-700">社区正式名称</div>
            <Input
              value={nameDraft}
              onChange={event => setNameDraft(event.target.value)}
              placeholder="请输入社区正式名称"
            />
            <p className="mt-2 text-sm text-slate-500">
              修改后所属部门会同步改名，旧名称会自动保留为别名。
            </p>
          </div>
          <div>
            <div className="mb-2 font-medium text-slate-700">社区民警</div>
            <p className="mb-3 text-sm text-slate-500">
              输入姓名后按回车添加。可以添加多位，工作日志中会用“、”连接。
            </p>
            <Select
              mode="tags"
              value={officerDraft}
              onChange={setOfficerDraft}
              tokenSeparators={[',', '，', '、']}
              placeholder="例如：张三"
              className="w-full"
              maxTagCount="responsive"
              options={[]}
            />
          </div>
          <div>
            <div className="mb-2 font-medium text-slate-700">社区别名</div>
            <p className="mb-3 text-sm text-slate-500">
              按来源数据中的完整名称填写，按回车添加。例如正式名称为“南厍”时，可以添加别名“南厍村”。系统不会自动删除“社区”或“村”。
            </p>
            <Select
              mode="tags"
              value={aliasDraft}
              onChange={setAliasDraft}
              tokenSeparators={[',', '，']}
              placeholder="例如：芦荡"
              className="w-full"
              maxTagCount="responsive"
              options={[]}
            />
          </div>
        </div>
      </Modal>}
    </div>
  )
}
