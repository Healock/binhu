import { useState, useEffect, useCallback } from 'react'
import { getGridCommunities, addGridCommunity, deleteGridCommunity } from '../api/client'
import { getDisplayMode } from '../utils/displayMode'

interface Community { id: number; name: string; grid_count: number }

export default function Communities() {
  const [communities, setCommunities] = useState<Community[]>([])
  const [newName, setNewName] = useState('')
  const [msg, setMsg] = useState('')

  const fetch = useCallback(async () => {
    try { setCommunities(await getGridCommunities()) } catch {}
  }, [])

  useEffect(() => { fetch() }, [fetch])

  const handleAdd = async () => {
    if (!newName.trim()) return
    try { await addGridCommunity(newName.trim()); setNewName(''); setMsg(''); fetch() }
    catch (e: any) { setMsg(e?.response?.data?.detail || '添加失败') }
  }

  const handleDelete = async (id: number, name: string) => {
    if (!confirm(`确认删除社区"${name}"？`)) return
    try { await deleteGridCommunity(id); fetch() } catch {}
  }

  return (
    <div className="space-y-4">
      <div className="bg-white rounded-lg shadow p-4">
        <div className="flex flex-wrap gap-3 items-center">
          <input value={newName} onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleAdd()}
            placeholder="输入社区名" className="border border-gray-300 rounded px-3 py-1.5 text-sm flex-1 min-w-32" />
          <button onClick={handleAdd} className="px-4 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700">添加</button>
          <span className="text-sm text-gray-500 ml-auto">共 {communities.length} 个社区</span>
        </div>
        {msg && <p className="text-sm text-gray-500 mt-2">{msg}</p>}
      </div>

      <div className="bg-white rounded-lg shadow overflow-auto">
        {communities.length === 0 ? (
          <p className="p-8 text-center text-gray-400 text-sm">暂无社区，点击"从数据导入"从原始数据提取</p>
        ) : getDisplayMode() === 'card' ? (
          <div className="grid grid-cols-1 gap-3 p-4">
            {communities.map((c) => (
              <div key={c.id} className="border border-gray-200 rounded-lg p-4 flex items-center justify-between">
                <div>
                  <div className="font-medium text-gray-800">{c.name}</div>
                  <div className="text-sm text-gray-500">网格员 {c.grid_count} 人</div>
                </div>
                <button onClick={() => handleDelete(c.id, c.name)} className="text-xs text-red-500 hover:underline">删除</button>
              </div>
            ))}
          </div>
        ) : (
          <table className="min-w-full text-sm">
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
                  <td className="px-3 py-2"><button onClick={() => handleDelete(c.id, c.name)} className="text-xs text-red-500 hover:underline">删除</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <p className="text-xs text-gray-400">网格员人数由网格员管理中每个网格员所属社区自动统计，无需手动维护。</p>
    </div>
  )
}
