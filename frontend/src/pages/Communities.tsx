import { useState, useEffect, useCallback } from 'react'
import { Alert, Button, Input, Modal, Select, Tag } from 'antd'
import type { TableColumnsType } from 'antd'
import { EditOutlined, PlusOutlined } from '@ant-design/icons'
import {
  getGridCommunities,
  addGridCommunity,
  deleteGridCommunity,
  updateGridCommunityAliases,
  type GridCommunity,
} from '../api/client'
import AppTable from '../components/AppTable'
import { EmptyState, LoadingState, PageHeader } from '../components/ui'

export default function Communities() {
  const [communities, setCommunities] = useState<GridCommunity[]>([])
  const [newName, setNewName] = useState('')
  const [msg, setMsg] = useState('')
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [editingAliases, setEditingAliases] = useState<GridCommunity | null>(null)
  const [aliasDraft, setAliasDraft] = useState<string[]>([])
  const [savingAliases, setSavingAliases] = useState(false)

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

  const openAliasEditor = (community: GridCommunity) => {
    setEditingAliases(community)
    setAliasDraft(community.aliases || [])
  }

  const handleSaveAliases = async () => {
    if (!editingAliases) return
    setSavingAliases(true)
    try {
      const result = await updateGridCommunityAliases(
        editingAliases.id,
        aliasDraft,
      )
      const matchedText = result.matched_visit_rows > 0
        ? `，同时归类 ${result.matched_visit_rows} 条已有走访数据`
        : ''
      setMsg(`“${editingAliases.name}”的别名已保存${matchedText}`)
      setEditingAliases(null)
      setAliasDraft([])
      await fetch()
    } catch (error: any) {
      setMsg(`保存失败：${error?.response?.data?.detail || '请稍后重试'}`)
    } finally {
      setSavingAliases(false)
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
    {
      title: '操作',
      key: 'actions',
      width: 190,
      render: (_, community) => (
        <div className="flex items-center gap-1">
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={() => openAliasEditor(community)}
          >
            编辑别名
          </Button>
          <Button type="link" danger size="small" onClick={() => handleDelete(community.id, community.name)}>
            删除
          </Button>
        </div>
      ),
    },
  ]

  return (
    <div className="app-page">
      <PageHeader
        title="社区管理"
        description="维护社区名单，并查看每个社区的人员数量"
        actions={<Tag color="blue">共 {communities.length} 个社区</Tag>}
      />

      <section className="app-card">
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
      </section>

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
                    <div className="mt-2 flex flex-wrap gap-1">
                      {c.aliases?.length > 0
                        ? c.aliases.map(alias => <Tag key={alias}>{alias}</Tag>)
                        : <span className="text-xs text-slate-400">暂无别名</span>}
                    </div>
                  </div>
                  <div className="flex flex-col items-end">
                    <Button type="link" size="small" onClick={() => openAliasEditor(c)}>编辑别名</Button>
                    <Button type="link" danger size="small" onClick={() => handleDelete(c.id, c.name)}>删除</Button>
                  </div>
                </div>
              ))}
            </div>
          </div>
          <div className="hidden md:block">
            <AppTable<GridCommunity>
              columns={communityColumns}
              dataSource={communities}
              rowKey="id"
              scroll={{ x: 520 }}
            />
          </div>
        </>
      )}
      <p className="text-xs text-slate-500">人员数量会根据“人员管理”中的所属社区自动统计，无需手动填写。</p>

      <Modal
        open={Boolean(editingAliases)}
        title={editingAliases ? `编辑“${editingAliases.name}”的别名` : '编辑社区别名'}
        okText="保存"
        cancelText="取消"
        confirmLoading={savingAliases}
        onOk={handleSaveAliases}
        onCancel={() => {
          setEditingAliases(null)
          setAliasDraft([])
        }}
      >
        <p className="mb-3 text-sm text-slate-500">
          输入来源数据里可能出现的其他名称，按回车添加。末尾的“社区”或“村”会自动去掉。
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
      </Modal>
    </div>
  )
}
