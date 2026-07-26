import { useState, useEffect, useCallback } from 'react'
import { getQueryTypes, queryData } from '../api/client'

export default function DataQuery() {
  const [types, setTypes] = useState<string[]>([])
  const [selectedType, setSelectedType] = useState('全链条')
  const [source, setSource] = useState<'online' | 'archive'>('online')
  const [keyword, setKeyword] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [data, setData] = useState<Record<string, string>[]>([])
  const [columns, setColumns] = useState<string[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize] = useState(50)
  const [loading, setLoading] = useState(false)
  const [sortCol, setSortCol] = useState<string | null>(null)
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [filters, setFilters] = useState<Record<string, string[]>>({})
  const [filterOpenCol, setFilterOpenCol] = useState<string | null>(null)

  useEffect(() => { getQueryTypes().then(setTypes).catch(() => {}) }, [])

  // 有筛选值的列
  const activeFilterCount = Object.values(filters).filter((v) => v.length > 0).length

  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      const activeFilters: Record<string, string[]> = {}
      for (const [k, v] of Object.entries(filters)) {
        if (v.length > 0) activeFilters[k] = v
      }
      const result = await queryData({
        type: selectedType, source, page, page_size: pageSize,
        keyword: keyword || undefined,
        sort_by: sortCol || undefined, sort_order: sortDir,
        filters: activeFilterCount > 0 ? activeFilters : undefined,
      })
      setData(result.data); setColumns(result.columns); setTotal(result.total)
    } catch (e) { console.error('查询失败', e) }
    finally { setLoading(false) }
  }, [selectedType, source, page, pageSize, keyword, sortCol, sortDir, filters, activeFilterCount])

  useEffect(() => { setPage(1); fetchData() }, [selectedType, source, keyword, sortCol, sortDir, filters])
  useEffect(() => { fetchData() }, [page])

  const handleSort = (col: string) => {
    if (sortCol === col) setSortDir(sortDir === 'asc' ? 'desc' : 'asc')
    else { setSortCol(col); setSortDir('asc') }
  }

  // 每列唯一值（基于当前页数据，用于下拉选项）
  const getUniqueValues = (col: string): string[] => {
    const set = new Set<string>()
    for (const row of data) set.add(row[col] || '(空)')
    return Array.from(set).sort()
  }

  const toggleFilter = (col: string, value: string) => {
    setFilters((prev) => {
      const cur = prev[col] || []
      const next = cur.includes(value) ? cur.filter((v) => v !== value) : [...cur, value]
      return { ...prev, [col]: next }
    })
  }

  const clearFilter = (col: string) => {
    setFilters((prev) => { const n = { ...prev }; delete n[col]; return n })
  }

  const totalPages = Math.ceil(total / pageSize)

  return (
    <div className="space-y-4">
      <div className="bg-white rounded-lg shadow p-4">
        <div className="flex flex-wrap gap-3 items-center">
          <select value={selectedType} onChange={(e) => setSelectedType(e.target.value)}
            className="border border-gray-300 rounded px-3 py-1.5 text-sm">
            {types.map((t) => (<option key={t} value={t}>{t}</option>))}
          </select>
          <div className="flex rounded border border-gray-300 overflow-hidden">
            <button onClick={() => setSource('online')}
              className={`px-3 py-1.5 text-sm ${source === 'online' ? 'bg-blue-600 text-white' : 'bg-white text-gray-600'}`}>当前数据</button>
            <button onClick={() => setSource('archive')}
              className={`px-3 py-1.5 text-sm ${source === 'archive' ? 'bg-blue-600 text-white' : 'bg-white text-gray-600'}`}>归档数据</button>
          </div>
          <input placeholder="搜索..." value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && setKeyword(searchInput)}
            className="border border-gray-300 rounded px-3 py-1.5 text-sm flex-1 min-w-32" />
          <button onClick={() => setKeyword(searchInput)}
            className="px-4 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700">搜索</button>
          <span className="text-sm text-gray-500 ml-auto">
            共 {total} 条{activeFilterCount > 0 && ` · ${activeFilterCount} 列筛选中`}
          </span>
        </div>
      </div>

      <div className="bg-white rounded-lg shadow overflow-auto">
        {loading ? (
          <p className="text-sm text-gray-400 p-8 text-center">加载中...</p>
        ) : data.length === 0 ? (
          <p className="text-sm text-gray-400 p-8 text-center">暂无数据</p>
        ) : (
          <table className="min-w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200 sticky top-0">
              <tr>
                {columns.map((col) => (
                  <th key={col} className="px-3 py-2 text-left font-medium text-gray-600 whitespace-nowrap select-none">
                    <div className="flex items-center gap-1">
                      <button onClick={() => handleSort(col)} className="hover:text-blue-600 flex items-center gap-0.5">
                        {col}
                        {sortCol === col && <span className="text-blue-600">{sortDir === 'asc' ? '↑' : '↓'}</span>}
                      </button>
                      <div className="relative">
                        <button onClick={() => setFilterOpenCol(filterOpenCol === col ? null : col)}
                          className={`text-xs px-1 rounded ${filters[col]?.length > 0 ? 'text-blue-600 font-bold' : 'text-gray-400 hover:text-gray-600'}`}>▼</button>
                        {filterOpenCol === col && (
                          <div className="absolute z-20 mt-1 bg-white border border-gray-200 rounded shadow-lg max-h-60 overflow-auto w-40">
                            <div className="flex justify-between items-center px-2 py-1 border-b border-gray-100">
                              <span className="text-xs text-gray-500">筛选</span>
                              <button onClick={() => clearFilter(col)} className="text-xs text-red-500 hover:underline">清除</button>
                            </div>
                            {getUniqueValues(col).map((val) => (
                              <label key={val} className="flex items-center px-2 py-1 hover:bg-gray-50 cursor-pointer">
                                <input type="checkbox" checked={filters[col]?.includes(val) || false}
                                  onChange={() => toggleFilter(col, val)} className="mr-2" />
                                <span className="text-xs truncate">{val}</span>
                              </label>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {data.map((row, i) => (
                <tr key={i} className="hover:bg-gray-50">
                  {columns.map((col) => (
                    <td key={col} className="px-3 py-2 text-gray-800 whitespace-nowrap max-w-48 truncate" title={row[col]}>
                      {row[col] || '-'}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-2">
          <button onClick={() => setPage(Math.max(1, page - 1))} disabled={page === 1}
            className="px-3 py-1.5 border border-gray-300 rounded text-sm hover:bg-gray-50 disabled:opacity-30">上一页</button>
          <span className="text-sm text-gray-600">{page} / {totalPages}</span>
          <button onClick={() => setPage(Math.min(totalPages, page + 1))} disabled={page === totalPages}
            className="px-3 py-1.5 border border-gray-300 rounded text-sm hover:bg-gray-50 disabled:opacity-30">下一页</button>
        </div>
      )}
    </div>
  )
}
