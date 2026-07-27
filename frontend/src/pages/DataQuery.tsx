import { useState, useEffect, useCallback } from 'react'
import { Button, Input, Segmented, Select, Tag, Tooltip } from 'antd'
import type { TableColumnsType, TableProps } from 'antd'
import { FilterOutlined, SearchOutlined } from '@ant-design/icons'
import { getQueryTypes, queryData } from '../api/client'
import AppTable from '../components/AppTable'
import { PageHeader } from '../components/ui'

type QueryRow = Record<string, string> & { __tableKey: string }

const EMPTY_FILTER_VALUE = '__binhu_empty_value__'

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
      setData([])
      setTotal(0)
    }
    finally { setLoading(false) }
  }, [selectedType, source, page, pageSize, keyword, sortCol, sortDir, filters, activeFilterCount])

  useEffect(() => { fetchData() }, [fetchData])

  // 每列唯一值（基于当前页数据，用于下拉选项）
  const getUniqueValues = (col: string): string[] => {
    const set = new Set<string>(filters[col] || [])
    for (const row of data) set.add(row[col] ?? '')
    return Array.from(set).sort((left, right) => left.localeCompare(right, 'zh-CN'))
  }

  const resetTableState = () => {
    setPage(1)
    setSortCol(null)
    setSortDir('desc')
    setFilters({})
  }

  const handleTypeChange = (value: string) => {
    setSelectedType(value)
    resetTableState()
  }

  const handleSourceChange = (value: 'online' | 'archive') => {
    setSource(value)
    resetTableState()
  }

  const handleSearch = () => {
    setKeyword(searchInput)
    setPage(1)
  }

  const handleTableChange: NonNullable<TableProps<QueryRow>['onChange']> = (
    pagination,
    nextFilters,
    sorter,
  ) => {
    setPage(pagination.current || 1)

    const activeSorter = Array.isArray(sorter) ? sorter[0] : sorter
    if (activeSorter?.order) {
      setSortCol(String(activeSorter.field || activeSorter.columnKey))
      setSortDir(activeSorter.order === 'ascend' ? 'asc' : 'desc')
    } else {
      setSortCol(null)
      setSortDir('desc')
    }

    const normalizedFilters: Record<string, string[]> = {}
    for (const [column, values] of Object.entries(nextFilters)) {
      if (!values?.length) continue
      normalizedFilters[column] = values.map(value => (
        String(value) === EMPTY_FILTER_VALUE ? '' : String(value)
      ))
    }
    setFilters(normalizedFilters)
  }

  const tableColumns: TableColumnsType<QueryRow> = columns.map(column => ({
    title: column,
    dataIndex: column,
    key: column,
    width: 180,
    sorter: true,
    sortOrder: sortCol === column
      ? (sortDir === 'asc' ? 'ascend' : 'descend')
      : null,
    filters: getUniqueValues(column).map(value => ({
      text: value || '(空)',
      value: value || EMPTY_FILTER_VALUE,
    })),
    filteredValue: filters[column]?.map(value => value || EMPTY_FILTER_VALUE) || null,
    ellipsis: { showTitle: false },
    render: value => (
      <Tooltip title={value || '-'}>
        <span>{value || '-'}</span>
      </Tooltip>
    ),
  }))

  const tableData: QueryRow[] = data.map((row, index) => ({
    ...row,
    __tableKey: `${page}-${index}`,
  }))

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
            onChange={handleTypeChange}
            className="min-w-44"
            options={types.map(type => ({ value: type, label: type }))}
          />
          <Segmented
            value={source}
            onChange={value => handleSourceChange(value as 'online' | 'archive')}
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
            onPressEnter={handleSearch}
            className="min-w-56 flex-1"
          />
          <Button type="primary" icon={<SearchOutlined />} onClick={handleSearch}>
            搜索
          </Button>
          {activeFilterCount > 0 && (
            <Tag icon={<FilterOutlined />} color="processing">{activeFilterCount} 列筛选中</Tag>
          )}
        </div>
      </section>

      <AppTable<QueryRow>
        columns={tableColumns}
        dataSource={tableData}
        emptyText={error || '没有找到符合条件的数据'}
        loading={{ spinning: loading, tip: '正在查询数据...' }}
        onChange={handleTableChange}
        pagination={{
          current: page,
          pageSize,
          total,
          hideOnSinglePage: true,
          showLessItems: true,
          showSizeChanger: false,
          showTotal: count => `共 ${count} 条`,
        }}
        rowKey="__tableKey"
        scroll={{ x: Math.max(columns.length * 180, 900) }}
      />
    </div>
  )
}
