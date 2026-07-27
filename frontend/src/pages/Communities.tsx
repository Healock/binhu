import { useState, useEffect, useCallback } from 'react'
import { Alert, Button, Input, Modal, Tag } from 'antd'
import type { TableColumnsType } from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import { getGridCommunities, addGridCommunity, deleteGridCommunity } from '../api/client'
import AppTable from '../components/AppTable'
import { getDisplayMode } from '../utils/displayMode'
import { EmptyState, LoadingState, PageHeader } from '../components/ui'

interface Community { id: number; name: string; grid_count: number }

export default function Communities() {
  const [communities, setCommunities] = useState<Community[]>([])
  const [newName, setNewName] = useState('')
  const [msg, setMsg] = useState('')
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')

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

  const communityColumns: TableColumnsType<Community> = [
    {
      title: '社区名称',
      dataIndex: 'name',
      key: 'name',
      width: 240,
      sorter: (left, right) => left.name.localeCompare(right.name, 'zh-CN'),
      render: value => <span className="font-medium text-slate-800">{value}</span>,
    },
    {
      title: '网格员人数',
      dataIndex: 'grid_count',
      key: 'grid_count',
      width: 160,
      sorter: (left, right) => left.grid_count - right.grid_count,
    },
    {
      title: '操作',
      key: 'actions',
      width: 120,
      render: (_, community) => (
        <Button type="link" danger size="small" onClick={() => handleDelete(community.id, community.name)}>
          删除
        </Button>
      ),
    },
  ]

  return (
    <div className="app-page">
      <PageHeader
        title="社区管理"
        description="维护社区名单，并查看每个社区的网格员人数"
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
      ) : getDisplayMode() === 'card' ? (
        <div className="app-table-wrap">
          <div className="grid grid-cols-1 gap-3 p-4">
            {communities.map((c) => (
              <div key={c.id} className="border border-gray-200 rounded-lg p-4 flex items-center justify-between">
                <div>
                  <div className="font-medium text-gray-800">{c.name}</div>
                  <div className="text-sm text-gray-500">网格员 {c.grid_count} 人</div>
                </div>
                <Button type="link" danger size="small" onClick={() => handleDelete(c.id, c.name)}>删除</Button>
              </div>
            ))}
          </div>
        </div>
      ) : (
        <AppTable<Community>
          columns={communityColumns}
          dataSource={communities}
          rowKey="id"
          scroll={{ x: 520 }}
        />
      )}
      <p className="text-xs text-slate-500">网格员人数会根据“网格员管理”中的所属社区自动统计，无需手动填写。</p>
    </div>
  )
}
