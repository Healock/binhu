import { useState, useEffect, useCallback } from 'react'
import { Button, Input, Pagination, Segmented, Select, Tag } from 'antd'
import { FilterOutlined, SearchOutlined } from '@ant-design/icons'
import { getQueryTypes, queryData } from '../api/client'
import { EmptyState, LoadingState, PageHeader } from '../components/ui'

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
  const [error, setError] = useState('')
  const [sortCol, setSortCol] = useState<string | null>(null)
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [filters, setFilters] = useState<Record<string, string[]>>({})
  const [filterOpenCol, setFilterOpenCol] = useState<string | null>(null)

  useEffect(() => { getQueryTypes().then(setTypes).catch(() => {}) }, [])

  // 有筛选值的列
  const activeFilterCount = Object.values(filters).filter((v) => v.length > 0).length

  const fetchData = useCallback(async () => {
    setLoading(true)
    setError('')
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
    } catch (e) {
      console.error('查询失败', e)
      setError('查询失败，请检查网络后重试')
    }
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
    <div className="app-page">
      <PageHeader
        title="在线数据查询"
        description="查询当前数据或历史归档，支持关键词、列筛选和排序"
        actions={<Tag color="blue">共 {total} 条</Tag>}
      />

      <section className="app-card">
        <div className="app-toolbar">
          <Select
            value={selectedType}
            onChange={setSelectedType}
            className="min-w-44"
            options={types.map(type => ({ value: type, label: type }))}
          />
          <Segmented
            value={source}
            onChange={value => setSource(value as 'online' | 'archive')}
            options={[
              { value: 'online', label: '当前数据' },
              { value: 'archive', label: '归档数据' },
            ]}
          />
          <Input
            allowClear
            prefix={<SearchOutlined className="text-slate-400" />}
            placeholder="输入关键词搜索"
            value={searchInput}
            onChange={event => setSearchInput(event.target.value)}
            onPressEnter={() => setKeyword(searchInput)}
            className="min-w-56 flex-1"
          />
          <Button type="primary" icon={<SearchOutlined />} onClick={() => setKeyword(searchInput)}>
            搜索
          </Button>
          {activeFilterCount > 0 && (
            <Tag icon={<FilterOutlined />} color="processing">{activeFilterCount} 列筛选中</Tag>
          )}
        </div>
      </section>

      <div className="app-table-wrap">
        {loading ? (
          <LoadingState label="正在查询数据..." />
        ) : error ? (
          <EmptyState label={error} />
        ) : data.length === 0 ? (
          <EmptyState label="没有找到符合条件的数据" />
        ) : (
          <table className="app-table min-w-full">
            <thead className="bg-gray-50 border-b border-gray-200 sticky top-0">
              <tr>
                {columns.map((col) => (
                  <th key={col} className="px-3 py-2 text-left font-medium text-gray-600 whitespace-nowrap select-none">
                    <div className="flex items-center gap-1">
                      <button onClick={() => handleSort(col)} className="compact-action flex items-center gap-0.5 hover:text-blue-600">
                        {col}
                        {sortCol === col && <span className="text-blue-600">{sortDir === 'asc' ? '↑' : '↓'}</span>}
                      </button>
                      <div className="relative">
                        <button onClick={() => setFilterOpenCol(filterOpenCol === col ? null : col)}
                          aria-label={`筛选${col}`}
                          className={`compact-action rounded px-1 text-xs ${filters[col]?.length > 0 ? 'font-bold text-blue-600' : 'text-gray-400 hover:text-gray-600'}`}>▼</button>
                        {filterOpenCol === col && (
                          <div className="absolute z-20 mt-1 bg-white border border-gray-200 rounded shadow-lg max-h-60 overflow-auto w-40">
                            <div className="flex justify-between items-center px-2 py-1 border-b border-gray-100">
                              <span className="text-xs text-gray-500">筛选</span>
                              <button onClick={() => clearFilter(col)} className="compact-action text-xs text-red-600 hover:underline">清除</button>
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

      {totalPages > 1 && !loading && (
        <div className="flex justify-center">
          <Pagination
            current={page}
            pageSize={pageSize}
            total={total}
            showSizeChanger={false}
            showLessItems
            onChange={setPage}
          />
        </div>
      )}
    </div>
  )
}
