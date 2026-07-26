import { useState, useEffect, useCallback } from 'react'
import { Alert, Button, Input, Modal, Tag } from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import { getGridCommunities, addGridCommunity, deleteGridCommunity } from '../api/client'
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

      <div className="app-table-wrap">
        {loading ? (
          <LoadingState />
        ) : loadError ? (
          <EmptyState label={loadError} />
        ) : communities.length === 0 ? (
          <EmptyState label="暂无社区，可在上方输入社区名称后添加" />
        ) : getDisplayMode() === 'card' ? (
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
        ) : (
          <table className="app-table min-w-full">
            <thead className="bg-gray-50 border-b sticky top-0 z-10">
              <tr>
                <th className="px-3 py-2 text-left font-medium text-gray-600">社区名称</th>
                <th className="px-3 py-2 text-left font-medium text-gray-600">网格员人数</th>
                <th className="px-3 py-2 text-left font-medium text-gray-600">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {communities.map((c) => (
                <tr key={c.id} className="hover:bg-gray-50">
                  <td className="px-3 py-2 text-gray-800">{c.name}</td>
                  <td className="px-3 py-2 text-gray-700">{c.grid_count}</td>
                  <td className="px-3 py-2"><Button type="link" danger size="small" onClick={() => handleDelete(c.id, c.name)}>删除</Button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <p className="text-xs text-slate-500">网格员人数会根据“网格员管理”中的所属社区自动统计，无需手动填写。</p>
    </div>
  )
}
